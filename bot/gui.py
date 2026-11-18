"""Tkinter desktop GUI for the Indeed HK auto-apply bot.

Run it from the project root with::

    python -m bot.gui

The GUI is a thin front end over the same helpers the CLI uses. Every action
runs in a daemon worker thread that owns its own Playwright session, SQLite
store and answer engine, because Playwright's sync API cannot be shared across
threads. The window itself never touches Playwright.
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import yaml

from .browser_setup import browsers_installed, cdp_chrome_running, launch_cdp_chrome
from .runtime import CONFIG_PATH, DATA_DIR, ENV_PATH, ROOT
from .session import IndeedSession
from .storage import Store
from .utils import (
    DEFAULT_CONFIG,
    ChallengeDetected,
    ConfigError,
    load_config,
    setup_logging,
)

try:  # validation helper is private but stable; the GUI only needs the check
    from .utils import _validate as _validate_config
except Exception:  # pragma: no cover - older utils builds
    _validate_config = None

try:
    from .answers import AnswerEngine
except Exception as _answers_error:  # pragma: no cover - optional at import time
    AnswerEngine = None  # type: ignore[assignment]
    ANSWERS_IMPORT_ERROR: Exception | None = _answers_error
else:
    ANSWERS_IMPORT_ERROR = None

try:
    from .reviewer import show_table, write_preview_csv
except Exception:  # pragma: no cover - rich missing must not break the GUI
    show_table = None  # type: ignore[assignment]
    write_preview_csv = None  # type: ignore[assignment]

# ``main.py`` lives in the project root, not in the package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from main import apply_jobs_selected, collect_jobs
except Exception as _main_error:  # pragma: no cover - missing dependencies
    apply_jobs_selected = None  # type: ignore[assignment]
    collect_jobs = None  # type: ignore[assignment]
    MAIN_IMPORT_ERROR: Exception | None = _main_error
else:
    MAIN_IMPORT_ERROR = None


#: Editable configuration rows: (label, dotted path, kind).
CONFIG_FIELDS = (
    ("Search query", "search.query", "str"),
    ("Location", "search.location", "str"),
    ("Max pages", "search.max_pages", "int"),
    ("Minimum salary (HKD per hour, 0 = any)", "filters.min_salary_hkd", "int"),
    ("Include keywords (comma separated)", "filters.keywords_include", "list"),
    ("Exclude keywords (comma separated)", "filters.keywords_exclude", "list"),
    ("Max applications per run", "limits.max_applications_per_run", "int"),
    ("Delay min (seconds)", "delays.apply_action.0", "float"),
    ("Delay max (seconds)", "delays.apply_action.1", "float"),
    ("Answer model", "answers.model", "str"),
    ("AI answers enabled", "answers.enabled", "bool"),
    ("Chrome CDP URL (blank = Playwright Chromium)", "browser.cdp_url", "str"),
)

#: Columns shown in the job table: (column id, heading, width).
TABLE_COLUMNS = (
    ("sel", "", 40),
    ("title", "Job title", 320),
    ("company", "Company", 180),
    ("salary", "Salary", 120),
    ("location", "Location", 190),
    ("posted", "Posted", 100),
)


def _get_path(cfg: dict, path: str):
    """Read a dotted path such as ``delays.apply_action.0`` from *cfg*."""
    current = cfg
    for part in path.split("."):
        current = current[int(part)] if part.isdigit() else current[part]
    return current


def _set_path(cfg: dict, path: str, value) -> None:
    """Write *value* at a dotted path such as ``search.query``."""
    parts = path.split(".")
    current = cfg
    for part in parts[:-1]:
        current = current[int(part)] if part.isdigit() else current[part]
    last = parts[-1]
    if last.isdigit():
        current[int(last)] = value
    else:
        current[last] = value


def _mask_key(key: str) -> str:
    """Short fingerprint of a secret; the full value is never shown again."""
    key = (key or "").strip()
    if not key:
        return "not set"
    if len(key) <= 8:
        return "saved (hidden)"
    return f"{key[:4]}...{key[-4:]}"


def _read_env() -> dict[str, str]:
    """Parse ``.env`` into a dict without ever printing it."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_env_value(key: str, value: str) -> None:
    """Set *key* in ``.env`` while preserving every other line."""
    lines: list[str] = []
    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    output: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            existing = line.split("=", 1)[0].strip()
            if existing == key:
                if not replaced:
                    output.append(f"{key}={value}")
                    replaced = True
                continue
        output.append(line)
    if not replaced:
        output.append(f"{key}={value}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")


def _attach_log_queue(name: str, log_queue: queue.Queue) -> None:
    """Make sure logger *name* also feeds the GUI log pane.

    ``setup_logging`` only installs handlers once, so a logger that was created
    before the GUI existed still needs its queue handler.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        return
    from logging.handlers import QueueHandler

    for handler in logger.handlers:
        if isinstance(handler, QueueHandler) and getattr(handler, "queue", None) is log_queue:
            return
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def _scrollable(parent):
    """Canvas + vertical scrollbar wrapper used by the Config tab."""
    canvas = tk.Canvas(parent, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    canvas.configure(yscrollcommand=scrollbar.set)
    return inner


class App(tk.Tk):
    """Main window: Run, Config and Account tabs."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Indeed HK Auto Apply Bot")
        self.geometry("1180x800")
        self.minsize(960, 640)

        self.log_queue: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()
        self.log = setup_logging(log_queue=self.log_queue)
        # The bot modules log on their own named loggers.
        for name in ("indeed-bot", "applier", "answers"):
            _attach_log_queue(name, self.log_queue)

        self._config_error: Exception | None = None
        self.cfg: dict = self._load_cfg_safe()
        self.matches: list[dict] = []
        self.checks: dict[str, bool] = {}
        self.running = False
        self.stop_event = threading.Event()

        self._build_tabs()
        self._load_config_form()
        self._load_account_form()
        self._refresh_browser_status()
        self.log.info("App files: %s", ROOT)
        self.log.info("Data folder: %s", DATA_DIR)
        self.after(150, self._poll)
        if self._config_error is not None:
            self.after(120, self._warn_config_error)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ build
    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        run_tab = ttk.Frame(notebook)
        config_tab = ttk.Frame(notebook)
        account_tab = ttk.Frame(notebook)
        notebook.add(run_tab, text="Run")
        notebook.add(config_tab, text="Config")
        notebook.add(account_tab, text="Account")

        self._build_run_tab(run_tab)
        self._build_config_tab(config_tab)
        self._build_account_tab(account_tab)

    def _build_run_tab(self, parent) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 4))

        self.scrape_btn = ttk.Button(controls, text="Scrape jobs", command=lambda: self._start("scrape"))
        self.dry_btn = ttk.Button(controls, text="Dry run", command=lambda: self._start("dryrun"))
        self.select_all_btn = ttk.Button(controls, text="Select all", command=self._select_all, state="disabled")
        self.clear_btn = ttk.Button(controls, text="Clear", command=self._clear_selection, state="disabled")
        self.apply_btn = ttk.Button(controls, text="Apply selected", command=self._apply_selected)
        self.stop_btn = ttk.Button(controls, text="Stop", command=self._stop, state="disabled")
        self.chrome_btn = ttk.Button(controls, text="Launch Chrome", command=self._launch_chrome)

        for button in (
            self.scrape_btn,
            self.dry_btn,
            self.select_all_btn,
            self.clear_btn,
            self.apply_btn,
            self.stop_btn,
            self.chrome_btn,
        ):
            button.pack(side="left", padx=(0, 6))

        options = ttk.Frame(parent)
        options.pack(fill="x", pady=(0, 4))
        self.preview_var = tk.BooleanVar(value=False)
        self.preview_check = ttk.Checkbutton(
            options,
            text="Preview answers (log AI drafts, never send them)",
            variable=self.preview_var,
        )
        self.preview_check.pack(side="left")
        self.browser_status_var = tk.StringVar(value="Chrome: checking...")
        ttk.Label(options, textvariable=self.browser_status_var, foreground="#666666").pack(side="right")

        status_row = ttk.Frame(parent)
        status_row.pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table_frame,
            columns=[column for column, _heading, _width in TABLE_COLUMNS],
            show="headings",
            selectmode="none",
            height=13,
        )
        for column, heading, width in TABLE_COLUMNS:
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor="w", stretch=column == "title")
        self.tree.pack(side="left", fill="both", expand=True)
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        table_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=table_scroll.set)
        self.tree.bind("<Button-1>", self._toggle_check)

        log_frame = ttk.LabelFrame(parent, text="Activity log")
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=11, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _build_config_tab(self, parent) -> None:
        inner = _scrollable(parent)
        self.config_vars: dict[str, tk.Variable] = {}
        for row, (label, path, kind) in enumerate(CONFIG_FIELDS):
            ttk.Label(inner, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            if kind == "bool":
                variable: tk.Variable = tk.BooleanVar()
                widget: tk.Widget = ttk.Checkbutton(inner, variable=variable)
            else:
                variable = tk.StringVar()
                widget = ttk.Entry(inner, textvariable=variable, width=56)
            self.config_vars[path] = variable
            widget.grid(row=row, column=1, sticky="we", padx=8, pady=4)
        inner.columnconfigure(1, weight=1)

        buttons = ttk.Frame(inner)
        buttons.grid(row=len(CONFIG_FIELDS), column=0, columnspan=2, pady=12)
        ttk.Button(buttons, text="Save config", command=self._save_config).pack(side="left", padx=4)
        ttk.Button(buttons, text="Reload", command=self._load_config_form).pack(side="left", padx=4)

        ttk.Label(
            inner,
            text=(
                f"Saved to {CONFIG_PATH}. Comments in config.yaml are not preserved.\n"
                "The config is re-read at the start of every run, so no restart is needed."
            ),
            foreground="#666666",
            justify="left",
        ).grid(row=len(CONFIG_FIELDS) + 1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 12))

    def _build_account_tab(self, parent) -> None:
        inner = ttk.Frame(parent)
        inner.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(inner, text="Indeed account", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        ttk.Label(
            inner,
            text=(
                "Indeed sign-in is manual and always happens in the Chrome window.\n"
                "The bot never types your password and never answers a captcha: it opens\n"
                "the sign-in page, waits for you to finish, then saves the cookies to\n"
                f"{DATA_DIR / 'session.json'} so later runs stay signed in."
            ),
            foreground="#444444",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self.login_btn = ttk.Button(
            inner, text="Open sign-in page / Test login", command=self._test_login
        )
        self.login_btn.grid(row=2, column=0, sticky="w", pady=6)
        self.account_status = tk.StringVar(value="Not tested")
        ttk.Label(inner, textvariable=self.account_status).grid(row=2, column=1, sticky="w", padx=8)

        ttk.Separator(inner, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="we", pady=16
        )

        ttk.Label(inner, text="OpenRouter API key (free models)", font=("TkDefaultFont", 12, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            inner,
            text=(
                "Used only to draft answers to screening questions. Stored in the local\n"
                f"{ENV_PATH.name} file next to this app; the saved key is shown masked."
            ),
            foreground="#666666",
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.api_key_var = tk.StringVar(value="")
        ttk.Entry(inner, textvariable=self.api_key_var, show="*", width=46).grid(
            row=6, column=0, sticky="w", pady=6
        )
        ttk.Button(inner, text="Save key", command=self._save_api_key).grid(
            row=6, column=1, sticky="w", padx=8
        )
        self.key_status = tk.StringVar(value="not set")
        ttk.Label(inner, textvariable=self.key_status, foreground="#666666").grid(
            row=7, column=0, columnspan=2, sticky="w"
        )

    # ------------------------------------------------------------------ config
    def _load_cfg_safe(self) -> dict:
        """Load config.yaml, falling back to the defaults so the window opens."""
        try:
            cfg = load_config()
        except Exception as exc:
            self._config_error = exc
            return copy.deepcopy(DEFAULT_CONFIG)
        self._config_error = None
        return cfg

    def _warn_config_error(self) -> None:
        self.status_var.set("Config problem - see the activity log")
        messagebox.showerror(
            "Config",
            f"config.yaml could not be loaded:\n{self._config_error}\n\n"
            "The defaults are shown in the Config tab. Fix the values and click "
            "Save config to rewrite the file.",
        )

    def _load_config_form(self) -> None:
        self.cfg = self._load_cfg_safe()
        for label, path, kind in CONFIG_FIELDS:
            variable = self.config_vars.get(path)
            if variable is None:
                continue
            try:
                value = _get_path(self.cfg, path)
            except (KeyError, IndexError, TypeError):
                value = "" if kind != "bool" else False
            if kind == "bool":
                variable.set(bool(value))
            elif kind == "list":
                variable.set(", ".join(str(item) for item in (value or [])))
            else:
                variable.set("" if value is None else str(value))
        self._refresh_browser_status()

    def _save_config(self) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is running. Stop it before saving the config.")
            return
        try:
            cfg = load_config()
        except ConfigError as exc:
            overwrite = messagebox.askyesno(
                "Config",
                f"config.yaml is invalid:\n{exc}\n\nOverwrite it with the values shown?",
            )
            if not overwrite:
                return
            cfg = copy.deepcopy(DEFAULT_CONFIG)

        try:
            for label, path, kind in CONFIG_FIELDS:
                variable = self.config_vars.get(path)
                if variable is None:
                    continue
                if kind == "bool":
                    value = bool(variable.get())
                elif kind == "list":
                    raw = str(variable.get()).strip()
                    value = [item.strip() for item in raw.split(",") if item.strip()]
                elif kind == "int":
                    value = int(str(variable.get()).strip())
                elif kind == "float":
                    value = float(str(variable.get()).strip())
                else:
                    value = str(variable.get()).strip()
                _set_path(cfg, path, value)
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Config", f"Invalid value: {exc}")
            return

        try:
            low = float(_get_path(cfg, "delays.apply_action.0"))
            high = float(_get_path(cfg, "delays.apply_action.1"))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            messagebox.showerror("Config", f"Delay values must be numbers: {exc}")
            return
        if low > high:
            messagebox.showerror("Config", "Delay min must be less than or equal to delay max.")
            return

        if _validate_config is not None:
            try:
                _validate_config(cfg)
            except ConfigError as exc:
                messagebox.showerror("Config", str(exc))
                return

        try:
            CONFIG_PATH.write_text(
                yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("Config", f"Could not write {CONFIG_PATH}: {exc}")
            return

        self.cfg = cfg
        self._refresh_browser_status()
        self.log.info("Configuration saved to %s", CONFIG_PATH)
        messagebox.showinfo("Config", "Configuration saved.")

    # ----------------------------------------------------------------- account
    def _load_account_form(self) -> None:
        key = _read_env().get("OPENROUTER_API_KEY", "")
        self.key_status.set(_mask_key(key))
        self.api_key_var.set("")

    def _save_api_key(self) -> None:
        value = self.api_key_var.get().strip()
        if not value:
            messagebox.showinfo("OpenRouter", "Paste an API key first.")
            return
        try:
            _write_env_value("OPENROUTER_API_KEY", value)
        except OSError as exc:
            messagebox.showerror("OpenRouter", f"Could not write {ENV_PATH}: {exc}")
            return
        # New answer engines read the variable from the environment or .env.
        os.environ["OPENROUTER_API_KEY"] = value
        self.api_key_var.set("")
        self.key_status.set(_mask_key(value))
        self.log.info("OpenRouter API key saved to %s", ENV_PATH)

    def _test_login(self) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is already running.")
            return
        if not self._browser_available():
            return
        self.account_status.set("Opening the sign-in page...")
        self._start("login")

    # ------------------------------------------------------------------ browser
    def _cdp_url(self, cfg: dict | None = None) -> str:
        browser = (cfg or self.cfg).get("browser") or {}
        return str(browser.get("cdp_url") or "").strip()

    def _refresh_browser_status(self) -> None:
        cdp_url = self._cdp_url()
        if cdp_url:
            if cdp_chrome_running(cdp_url):
                self.browser_status_var.set(f"Chrome: ready at {cdp_url}")
            else:
                self.browser_status_var.set(f"Chrome: not running ({cdp_url})")
            return
        if browsers_installed():
            self.browser_status_var.set("Browser: Playwright Chromium installed")
        else:
            self.browser_status_var.set("Browser: Playwright Chromium missing")

    def _browser_available(self) -> bool:
        """Check the browser the session would actually use."""
        cdp_url = self._cdp_url()
        if cdp_url:
            if cdp_chrome_running(cdp_url):
                return True
            messagebox.showinfo(
                "Chrome required",
                f"No browser is listening at {cdp_url}.\n\n"
                "Click 'Launch Chrome' first (or fix the CDP URL in the Config tab). "
                "The bot does not start Playwright's own Chromium while a CDP URL "
                "is configured, because Indeed blocks it.",
            )
            return False
        if browsers_installed():
            return True
        messagebox.showinfo(
            "Browser required",
            "Playwright's Chromium is not installed.\n\n"
            "Run this once in a terminal:\n\n"
            "    python -m playwright install chromium",
        )
        return False

    def _launch_chrome(self) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is already running.")
            return
        self.cfg = self._load_cfg_safe()
        if not self._cdp_url():
            messagebox.showinfo(
                "Launch Chrome",
                "browser.cdp_url is empty, so the bot would launch Playwright's own "
                "Chromium, which Indeed often blocks.\n\n"
                "Set it in the Config tab first, for example:\n"
                "    http://127.0.0.1:9222",
            )
            return
        self._start("chrome")

    # ------------------------------------------------------------------ actions
    def _start(self, action: str) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is already running.")
            return
        self.cfg = self._load_cfg_safe()
        if action in ("scrape", "dryrun") and not self._browser_available():
            return

        messages = {
            "scrape": "Scraping jobs...",
            "dryrun": "Dry run: scraping jobs...",
            "chrome": "Launching Chrome...",
            "login": "Waiting for the manual Indeed sign-in...",
        }
        self.stop_event.clear()
        self._set_busy(True)
        self.status_var.set(messages.get(action, "Working..."))
        threading.Thread(target=self._worker, args=(action, None), daemon=True).start()

    def _apply_selected(self) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is already running.")
            return
        self.cfg = self._load_cfg_safe()
        selected = self._selected_jobs()
        if not selected:
            messagebox.showinfo("No selection", "Tick the jobs you want to apply to first.")
            return
        if not self._browser_available():
            return

        preview_only = bool(self.preview_var.get())
        payload = {"jobs": selected, "preview_only": preview_only}
        self.stop_event.clear()
        self._set_busy(True)
        if preview_only:
            self.status_var.set(f"Previewing answers for {len(selected)} job(s)...")
        else:
            self.status_var.set(f"Applying to {len(selected)} job(s)...")
        threading.Thread(target=self._worker, args=("apply", payload), daemon=True).start()

    def _stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current job...")

    def _worker(self, action: str, payload) -> None:
        """Run one action. Playwright objects live and die inside this thread."""
        session = None
        store = None
        try:
            cfg = load_config()
            self.ui_queue.put(("config", cfg))

            if action == "chrome":
                ok = launch_cdp_chrome(cfg, self.log)
                self.ui_queue.put(("chrome", (bool(ok), self._cdp_url(cfg))))
                return

            if action == "login":
                session = IndeedSession(cfg, log=self.log)
                session.start()
                if not session.is_logged_in():
                    session.login_interactive()
                self.ui_queue.put(("login_status", bool(session.is_logged_in())))
                return

            if action not in ("scrape", "dryrun", "apply"):
                raise ValueError(f"Unknown action: {action}")

            if action == "apply":
                if apply_jobs_selected is None or AnswerEngine is None:
                    raise RuntimeError(
                        "The apply modules could not be imported (Playwright or requests "
                        f"missing): {MAIN_IMPORT_ERROR or ANSWERS_IMPORT_ERROR}"
                    )
                jobs = list((payload or {}).get("jobs") or [])
                preview_only = bool((payload or {}).get("preview_only"))
                store = Store()
                answers = AnswerEngine(cfg, store)
                session = IndeedSession(cfg, log=self.log)
                session.start()
                session.ensure_login()
                try:
                    self.log.info("Already applied today (recorded): %d", store.applied_today_count())
                except Exception:  # a fresh or odd DB must not block the run
                    pass
                summary = apply_jobs_selected(
                    session,
                    cfg,
                    store,
                    jobs,
                    answers,
                    self.log,
                    stop_check=self.stop_event.is_set,
                    preview_only=preview_only,
                )
                self.ui_queue.put(("apply_done", summary))
                return

            if collect_jobs is None:
                raise RuntimeError(
                    f"The scraping modules could not be imported: {MAIN_IMPORT_ERROR}"
                )
            store = Store()
            store.init_db()
            session = IndeedSession(cfg, log=self.log)
            session.start()
            session.ensure_login()
            matches = collect_jobs(session, cfg, store, self.log)
            self.ui_queue.put(("matches", matches))

            if action == "dryrun":
                if matches:
                    if show_table is not None:
                        try:
                            show_table(matches)
                        except Exception as exc:  # no console attached
                            self.log.warning("Console preview unavailable: %s", exc)
                    if write_preview_csv is not None:
                        try:
                            csv_path = write_preview_csv(matches)
                            self.log.info(
                                "Dry run: %d match(es); preview saved to %s. "
                                "No applications sent.",
                                len(matches),
                                csv_path,
                            )
                        except Exception as exc:
                            self.log.warning("Could not write the preview CSV: %s", exc)
                    else:
                        self.log.info("Dry run: %d match(es). No applications sent.", len(matches))
                else:
                    self.log.info("Dry run: no jobs matched; nothing to preview.")
                self.ui_queue.put(("status", f"Dry run complete: {len(matches)} match(es)"))
            else:
                self.ui_queue.put(("status", f"Scrape complete: {len(matches)} match(es)"))

        except ChallengeDetected as exc:
            self.log.error("Security check detected: %s", exc)
            self.ui_queue.put(("challenge", str(exc) or "security check"))
        except ConfigError as exc:
            self.log.error("Config error: %s", exc)
            self.ui_queue.put(("status", "Config error - check the Config tab"))
        except RuntimeError as exc:
            self.log.error("%s", exc)
            message = str(exc).lower()
            if "login" in message or "sign" in message:
                self.ui_queue.put(("status", "Indeed sign-in not completed - see the activity log"))
            elif "import" in message or "playwright" in message:
                self.ui_queue.put(("status", "Missing dependency - see the activity log"))
            else:
                self.ui_queue.put(("status", "Error - see the activity log"))
        except Exception as exc:
            self.log.exception("Error: %s", exc)
            self.ui_queue.put(("status", "Error - see the activity log"))
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            self.ui_queue.put(("done", None))

    # ------------------------------------------------------------------ table
    def _selected_jobs(self) -> list[dict]:
        selected: list[dict] = []
        for item in self.tree.get_children():
            if not self.checks.get(item):
                continue
            try:
                index = int(item)
            except ValueError:
                continue
            if 0 <= index < len(self.matches):
                selected.append(self.matches[index])
        return selected

    def _set_all_checks(self, checked: bool) -> None:
        for item in self.tree.get_children():
            self.checks[item] = checked
            self.tree.set(item, "sel", "[x]" if checked else "[ ]")

    def _select_all(self) -> None:
        self._set_all_checks(True)
        self.status_var.set(f"Selected all {len(self.matches)} job(s)")

    def _clear_selection(self) -> None:
        self._set_all_checks(False)
        self.status_var.set("Selection cleared")

    def _toggle_check(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        checked = not self.checks.get(item, False)
        self.checks[item] = checked
        self.tree.set(item, "sel", "[x]" if checked else "[ ]")

    def _show_matches(self, matches: list[dict]) -> None:
        self.matches = list(matches or [])
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.checks = {}
        for index, job in enumerate(self.matches):
            iid = str(index)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "[ ]",
                    job.get("title", "") or "",
                    job.get("company", "") or "",
                    job.get("salary", "") or "",
                    job.get("location", "") or "",
                    job.get("posted", "") or "",
                ),
            )
            self.checks[iid] = False
        self._update_selection_buttons()

    # ------------------------------------------------------------------- state
    def _set_busy(self, busy: bool) -> None:
        self.running = busy
        state = "disabled" if busy else "normal"
        for button in (self.scrape_btn, self.dry_btn, self.apply_btn, self.chrome_btn):
            button.configure(state=state)
        self.login_btn.configure(state=state)
        self.preview_check.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self._update_selection_buttons()

    def _update_selection_buttons(self) -> None:
        state = "normal" if (self.matches and not self.running) else "disabled"
        self.select_all_btn.configure(state=state)
        self.clear_btn.configure(state=state)

    def _poll(self) -> None:
        while not self.log_queue.empty():
            record = self.log_queue.get_nowait()
            try:
                text = record.getMessage()
            except Exception:
                text = str(record)
            level = getattr(record, "levelno", logging.INFO)
            if level >= logging.ERROR:
                text = f"[ERROR] {text}"
            elif level >= logging.WARNING:
                text = f"[WARN] {text}"
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        while not self.ui_queue.empty():
            kind, payload = self.ui_queue.get_nowait()
            if kind == "matches":
                self._show_matches(payload)
            elif kind == "status":
                self.status_var.set(str(payload))
            elif kind == "config":
                self.cfg = payload
            elif kind == "challenge":
                self.status_var.set(
                    "Indeed security check - clear it manually in the Chrome window, then retry"
                )
                self.log.error(
                    "The run was stopped by a security check (%s). Solve it in the browser "
                    "window and try again; the bot never solves captchas.",
                    payload,
                )
            elif kind == "chrome":
                ok, cdp_url = payload
                if ok:
                    self.browser_status_var.set(f"Chrome: ready at {cdp_url}")
                    self.status_var.set("Chrome is ready")
                else:
                    self.browser_status_var.set(f"Chrome: not reachable ({cdp_url})")
                    self.status_var.set("Chrome could not be started - see the activity log")
            elif kind == "login_status":
                if payload:
                    self.account_status.set("Signed in to Indeed")
                    self.status_var.set("Signed in to Indeed")
                else:
                    self.account_status.set("Not signed in - finish the sign-in in Chrome")
                    self.status_var.set("Not signed in")
            elif kind == "apply_done":
                summary = payload or {}
                self.status_var.set(
                    "Apply finished: applied={applied} skipped={skipped} failed={failed}".format(
                        applied=summary.get("applied", 0),
                        skipped=summary.get("skipped", 0),
                        failed=summary.get("failed", 0),
                    )
                )
            elif kind == "done":
                self._set_busy(False)

        self.after(150, self._poll)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel("Quit", "A task is still running. Quit anyway?"):
                return
            self.stop_event.set()
        try:
            self.destroy()
        except Exception:
            pass


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

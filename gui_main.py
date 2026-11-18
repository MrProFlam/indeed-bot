"""Desktop GUI entry point for the Indeed HK auto-apply bot."""

from __future__ import annotations

import sys


def main() -> None:
    import os

    from bot.runtime import DATA_DIR, is_frozen

    # Playwright/Chromium lives in the user profile, not inside the EXE.
    if is_frozen():
        local = os.environ.get("LOCALAPPDATA")
        if local:
            os.environ.setdefault(
                "PLAYWRIGHT_BROWSERS_PATH", os.path.join(local, "ms-playwright")
            )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Hidden diagnostic: python gui_main.py --self-test
    if "--self-test" in sys.argv:
        result = "unknown"
        try:
            from bot.utils import load_config

            cfg = load_config()
            result = f"OK: config loaded for {cfg['site']['domain']}"
        except Exception as exc:
            result = f"FAIL: {exc}"
        (DATA_DIR / "selftest.txt").write_text(result, encoding="utf-8")
        return

    from bot.gui import App

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

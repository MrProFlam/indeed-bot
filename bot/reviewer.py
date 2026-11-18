"""Human review gate: rich preview table, CSV export and selection prompt."""

from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .filters import parse_selection
from .utils import DATA_DIR

PROMPT = "Select jobs to apply [1,3,5 | 1-5 | all | q]: "
MAX_ATTEMPTS = 3

#: Columns written to ``data/preview.csv``.
CSV_FIELDS = (
    "number",
    "jobkey",
    "title",
    "company",
    "location",
    "salary",
    "salary_min",
    "indeed_applyable",
    "posted",
    "url",
    "snippet",
)


def _console() -> Console:
    # Modern Windows console API handles Unicode (Chinese job titles); the
    # legacy cp1252 writer raises UnicodeEncodeError on them.
    return Console(legacy_windows=False)


def write_preview_csv(jobs: list[dict], path: str | Path | None = None) -> Path:
    """Write the candidate list to ``data/preview.csv`` (UTF-8 with BOM)."""
    target = Path(path) if path is not None else DATA_DIR / "preview.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for number, job in enumerate(jobs, 1):
            writer.writerow(
                {
                    "number": number,
                    "jobkey": job.get("jobkey", ""),
                    "title": job.get("title", ""),
                    "company": job.get("company", ""),
                    "location": job.get("location", ""),
                    "salary": job.get("salary", ""),
                    "salary_min": job.get("salary_min", 0),
                    "indeed_applyable": "yes" if job.get("indeed_applyable") else "no",
                    "posted": job.get("posted", ""),
                    "url": job.get("url", ""),
                    "snippet": job.get("snippet", ""),
                }
            )
    return target


def show_table(jobs: list[dict], title: str = "Matching Indeed HK jobs") -> None:
    """Print the candidate list as a rich table (no prompt, no CSV)."""
    console = _console()
    if not jobs:
        console.print("[yellow]No jobs matched.[/yellow]")
        return

    table = Table(title=title, show_lines=True)
    table.add_column("#", style="cyan", justify="right", no_wrap=True)
    table.add_column("Title", style="bold", max_width=42)
    table.add_column("Company", max_width=24)
    table.add_column("Location", max_width=20)
    table.add_column("Salary", max_width=18)
    table.add_column("Indeed Apply", max_width=12)
    table.add_column("Posted", max_width=14)

    for number, job in enumerate(jobs, 1):
        table.add_row(
            str(number),
            job.get("title", "") or "",
            job.get("company", "") or "",
            job.get("location", "") or "",
            job.get("salary", "") or "",
            "yes" if job.get("indeed_applyable") else "no",
            job.get("posted", "") or "",
        )
    console.print(table)


def preview(jobs: list[dict]) -> list[dict]:
    """Show a preview, export the CSV and return the jobs the user approved.

    ``q``/``quit`` or an unreadable selection after three attempts returns an
    empty list (nothing is applied).
    """
    console = _console()
    if not jobs:
        console.print("[yellow]No jobs matched.[/yellow]")
        return []

    csv_path = write_preview_csv(jobs)
    show_table(jobs)
    console.print(f"[dim]Full list saved to {csv_path}[/dim]")

    for _attempt in range(MAX_ATTEMPTS):
        try:
            raw = input(PROMPT)
        except (EOFError, OSError, KeyboardInterrupt):
            console.print("\n[yellow]No selection received - nothing selected.[/yellow]")
            return []
        try:
            numbers = parse_selection(raw, len(jobs))
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            continue

        if not numbers:
            console.print("[dim]Nothing selected.[/dim]")
            return []

        selected = [jobs[number - 1] for number in numbers]
        console.print(f"[green]Selected {len(selected)} job(s).[/green]")
        return selected

    console.print("[yellow]Too many invalid answers - nothing selected.[/yellow]")
    return []

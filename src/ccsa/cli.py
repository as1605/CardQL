from __future__ import annotations

import logging
import typer
from rich.console import Console

from .config import ensure_local_dirs, load_config, write_config_templates
from .paths import get_paths
from .imap import fetch_pdfs

app = typer.Typer(help="Credit card statement analyzer CLI.")
console = Console()


def _configure_logging() -> None:
    """Configure ccsa logger; use env CCSA_LOG=DEBUG for verbose output."""
    level_name = __import__("os").environ.get("CCSA_LOG", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log = logging.getLogger("ccsa")
    log.setLevel(level)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setLevel(level)
        log.addHandler(h)
    log.propagate = False


imap_app = typer.Typer(help="IMAP: fetch statement PDFs.")
app.add_typer(imap_app, name="imap")


@app.callback()
def main() -> None:
    """Fetch statements, normalize transactions, and export for analysis."""


@app.command()
def init() -> None:
    """Initialize local (gitignored) config and data folders."""
    paths = ensure_local_dirs()
    write_config_templates(paths)
    console.print("[green]Initialized local folders[/green]")
    console.print(f"- Config: {paths.local_config_dir}")
    console.print(f"- State: {paths.local_state_dir}")
    console.print(f"- Raw PDFs: {paths.raw_pdfs_dir}")
    console.print(f"- Normalized: {paths.normalized_dir}")
    console.print(f"- Exports: {paths.exports_dir}")
    console.print("")
    console.print(
        "Next: edit [bold]secrets.json[/bold] (IMAP credentials) and "
        f"[bold]{paths.local_config_dir / 'card_rules.json'}[/bold] (bank/card → from_emails, passwords)."
    )


@imap_app.command("fetch")
def imap_fetch() -> None:
    """Fetch new statement PDFs via IMAP into data/raw-pdfs/ (per card_rules)."""
    _configure_logging()
    paths = get_paths()
    loaded = load_config(paths)
    if not loaded.config.email_rules:
        console.print("[yellow]No email rules. Add entries to .local/config/card_rules.json[/yellow]")
        raise SystemExit(1)
    try:
        result = fetch_pdfs(paths)
        if result.folder:
            console.print(f"[dim]Folder: {result.folder}[/dim]")
        if result.reunlocked:
            console.print(f"[cyan]Reunlocked {result.reunlocked} previously locked PDF(s).[/cyan]")
        console.print(
            f"[green]Downloaded {result.downloaded} new PDF(s).[/green] "
            f"Skipped {result.skipped} already-fetched."
        )
        for s in result.rule_summaries:
            console.print(
                f"  {s.bank}/{s.card}: found {s.found}, skipped {s.skipped}, downloaded {s.downloaded}"
                + (f", reunlocked {s.reunlocked}" if s.reunlocked else "")
            )
        for p in result.saved_paths:
            console.print(f"  [dim]{p}[/dim]")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    app()

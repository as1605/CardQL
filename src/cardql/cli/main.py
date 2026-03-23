from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape as rich_escape
from rich.table import Table

from ..config import ensure_local_dirs, load_config, resolve_password, write_config_templates
from ..ingest import fetch_pdfs, pdf as pdf_module, unlock_pdf
from ..parsers import get_parser, get_parsers_for_bank, try_parse_with_bank
from ..paths import get_paths
from .helpers import (
    configure_logging,
    console,
    launch_sqlite3_repl,
    month_from_stem,
    months_in_range,
    streamlit_app_path,
)
from . import pipeline

app = typer.Typer(help="CardQL — chat with your credit card statements.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", "-f", help="Re-normalize all PDFs even if JSON exists"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Master CSV path (default: data/exports/master.csv)"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open master.csv after export"),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Skip IMAP fetch"),
    skip_ollama: bool = typer.Option(False, "--skip-ollama", help="Skip Ollama ensure/pull"),
    no_ui: bool = typer.Option(False, "--no-ui", help="Do not launch Streamlit after pipeline"),
) -> None:
    """Full stack: init, fetch, parse, export, Ollama, Streamlit (when no subcommand)."""
    if ctx.invoked_subcommand is not None:
        return
    pipeline.run_full_stack(
        force_normalize=force,
        output_csv=output,
        open_csv=not no_open,
        no_fetch=no_fetch,
        skip_ollama=skip_ollama,
        no_ui=no_ui,
    )


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
    console.print(
        "[dim]Chat:[/dim] [bold]pip install -r requirements.txt[/bold] then [bold]cardql ollama[/bold] and [bold]cardql ui[/bold]."
    )


@app.command()
def fetch() -> None:
    """Fetch new statement PDFs via IMAP into data/raw-pdfs/."""
    configure_logging()
    paths = get_paths()
    loaded = load_config(paths)
    if not loaded.config.email_rules:
        console.print("[yellow]No email rules. Add entries to .local/config/card_rules.json[/yellow]")
        raise typer.Exit(1)
    try:
        result = fetch_pdfs(paths)
        console.print()
        if result.folder:
            console.print(f"[dim]Folder[/dim]  [bold]{rich_escape(result.folder)}[/bold]")
        if result.reunlocked:
            console.print(
                f"[cyan]Reunlocked[/cyan] [bold]{result.reunlocked}[/bold] "
                "[dim]previously locked PDF(s)[/dim]"
            )
        console.print(
            f"[bold green]Downloaded[/bold green] [bold]{result.downloaded}[/bold] "
            f"[dim]new PDF(s) ·[/dim] [yellow]{result.skipped}[/yellow] [dim]already in state[/dim]"
        )
        if result.rule_summaries:
            tbl = Table(
                title="[bold]Per rule[/bold]",
                show_header=True,
                header_style="bold cyan",
                border_style="dim",
                pad_edge=False,
            )
            tbl.add_column("Bank / card", style="cyan", no_wrap=True)
            tbl.add_column("Found", justify="right", style="white")
            tbl.add_column("Skip", justify="right", style="yellow")
            tbl.add_column("New", justify="right", style="green")
            for s in result.rule_summaries:
                tbl.add_row(
                    rich_escape(f"{s.bank} / {s.card}"),
                    str(s.found),
                    str(s.skipped),
                    str(s.downloaded),
                )
            console.print(tbl)
        if result.saved_paths:
            console.print("[dim]Saved paths[/dim]")
            for p in result.saved_paths:
                console.print(f"  [dim]•[/dim] {rich_escape(p)}")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command()
def parse(
    pdf_path: Optional[Path] = typer.Argument(
        None,
        help="Optional: one PDF under data/raw-pdfs/<bank>/<card>/ (omit for bulk normalize + export)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Re-parse even if normalized JSON exists"),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="If set with a single PDF: write JSON only to this path (skip CSV export)",
    ),
    output_csv: Optional[Path] = typer.Option(
        None,
        "--csv",
        "-c",
        help="Master CSV path (default: data/exports/master.csv)",
    ),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open master.csv after export"),
) -> None:
    """Normalize PDFs, merge to master.csv + transactions.sqlite, open CSV (unless --no-open)."""
    if pdf_path is not None and output is not None:
        p = Path(pdf_path).resolve()
        if not p.exists():
            console.print(f"[red]File not found: {p}[/red]")
            raise typer.Exit(1)
        paths = get_paths()
        loaded = load_config(paths)
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            parts = rel.parts
            if len(parts) >= 2:
                bank_slug, card_slug = parts[0], parts[1]
                bank_name = bank_slug.title()
                card_name = card_slug.title()
            else:
                bank_slug, card_slug = "", ""
                bank_name = "Unknown"
                card_name = "Unknown"
        except ValueError:
            bank_slug, card_slug = "", ""
            bank_name = "Unknown"
            card_name = "Unknown"
        parser = get_parser(bank_slug, card_slug)
        if parser is None:
            console.print(
                f"[red]No parser for {bank_name}/{card_name}. Supported banks: axis, hdfc, hsbc, icici, indusind, sbi[/red]"
            )
            raise typer.Exit(1)
        password = resolve_password(loaded, bank_name, card_name)
        raw = p.read_bytes()
        data, _ = unlock_pdf(raw, password)
        text = pdf_module.extract_text_from_pdf(data)
        statement = try_parse_with_bank(
            bank_slug,
            text,
            source_pdf_path=p,
            bank_display=bank_name,
            card_display=card_name,
        )
        if statement is None:
            console.print(f"[red]All parser variants failed for {p}[/red]")
            raise typer.Exit(1)
        out_json = statement.model_dump_json(indent=2)
        Path(output).resolve().parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(out_json, encoding="utf-8")
        console.print(f"[green]Wrote {len(statement.transactions)} transactions to {output}[/green]")
        return

    pipeline.run_data_build(
        force_normalize=force,
        output_csv=output_csv,
        open_csv=not no_open,
        single_pdf=Path(pdf_path).resolve() if pdf_path else None,
    )


@app.command()
def ollama(
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Ollama model tag (default: CARDQL_OLLAMA_MODEL or qwen3.5:0.8b-q8_0)",
    ),
) -> None:
    """Ensure Ollama API is up and pull the default chat model."""
    if not pipeline.run_ollama_setup(model=model):
        raise typer.Exit(1)


@app.command()
def ui(
    port: int = typer.Option(8501, "--port", "-p", help="Streamlit port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Streamlit host"),
) -> None:
    """Launch Streamlit chat UI for natural-language queries over transactions."""
    import shutil
    import subprocess
    import sys

    if not shutil.which("streamlit"):
        console.print(
            "[red]streamlit not found.[/red] Install with [bold]pip install -r requirements.txt[/bold] (includes streamlit)."
        )
        raise typer.Exit(1)
    app_path = streamlit_app_path()
    console.print(f"[dim]Launching Streamlit on http://{host}:{port}[/dim]")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(port),
            "--server.address",
            host,
        ],
        check=False,
    )


@app.command()
def sql(
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        "-d",
        help="SQLite database (default: data/exports/transactions.sqlite)",
    ),
) -> None:
    """Open an interactive sqlite3 session on the transactions database."""
    paths = get_paths()
    ensure_local_dirs(paths)
    db = Path(db_path or (paths.exports_dir / "transactions.sqlite")).resolve()
    launch_sqlite3_repl(db)


@app.command()
def check(
    gaps_only: bool = typer.Option(
        False,
        "--gaps",
        help="Run only the month-gap check (default: all checks)",
    ),
    source: str = typer.Option("raw-pdfs", "--source", "-s", help="Where to look: raw-pdfs or normalized"),
) -> None:
    """Run validation checks (month gaps between statements; more checks later)."""
    _ = gaps_only  # reserved: when multiple checks exist, False runs all, True runs gap check only
    paths = get_paths()
    skip_substrings = ["terms", "conditions", "most-important", "tariff", "mitc"]
    if source == "normalized":
        base = paths.normalized_dir
        files = list(base.rglob("*.json"))
    else:
        base = paths.raw_pdfs_dir
        files = [p for p in base.rglob("*.pdf") if not any(s in p.stem.lower() for s in skip_substrings)]

    by_card: dict[tuple[str, str], set[str]] = {}
    for p in files:
        try:
            rel = p.relative_to(base)
            parts = rel.parts
            if len(parts) < 2:
                continue
            bank_slug, card_slug = parts[0], parts[1]
            ym = month_from_stem(p.stem)
            if ym is None:
                continue
            key = (bank_slug, card_slug)
            by_card.setdefault(key, set()).add(ym)
        except ValueError:
            continue

    any_gaps = False
    for (bank_slug, card_slug), months in sorted(by_card.items()):
        if len(months) < 2:
            continue
        start_ym = min(months)
        end_ym = max(months)
        expected = set(months_in_range(start_ym, end_ym))
        missing = sorted(expected - months)
        if not missing:
            continue
        any_gaps = True
        console.print(
            f"[yellow]Gap(s) for {bank_slug}/{card_slug}[/yellow] "
            f"(range {start_ym}–{end_ym}): missing {', '.join(missing)}"
        )

    if not any_gaps:
        if not by_card:
            console.print("[dim]No statement files found. Add PDFs to data/raw-pdfs/<bank>/<card>/[/dim]")
        else:
            console.print(
                "[green]No gaps: every month is present between first and last statement for each card.[/green]"
            )

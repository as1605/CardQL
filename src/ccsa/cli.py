from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape as rich_escape
from rich.syntax import Syntax
from rich.table import Table

from .config import compute_tags, ensure_local_dirs, load_config, resolve_password, write_config_templates
from .paths import get_paths
from .imap import fetch_pdfs, _unlock_pdf
from . import pdf as pdf_module
from .parsers import get_parser, get_parsers_for_bank, try_parse_with_bank
from .parsers.schema import Statement, Transaction
from .sqlite_export import import_master_csv_to_sqlite

app = typer.Typer(help="Credit card statement analyzer CLI.")
console = Console()


def _open_file_default_app(path: Path) -> None:
    """Open a file with the OS default handler (same as double-click / ``open`` on macOS)."""
    path = Path(path).resolve()
    if not path.is_file():
        console.print(f"[yellow]Skip open: not a file: {path}[/yellow]")
        return
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as e:
        console.print(f"[yellow]Could not open {path}: {e}[/yellow]")


# Run via sqlite3 -cmd before the REPL (one -cmd per statement / dot-command).
_SQLITE3_STARTUP_CMDS: tuple[str, ...] = (
    ".headers on",
    ".mode table",
    "SELECT date,bank,card,description,amount,tags FROM transactions LIMIT 10;",
)


def _print_sqlite3_startup_plan() -> None:
    """Tell the user what sqlite3 will run internally (``-cmd``), especially the SQL."""
    console.print(
        "[bold]sqlite3[/bold] [dim]will run these first[/] [dim](inside sqlite3, not your shell):[/]"
    )
    for cmd in _SQLITE3_STARTUP_CMDS:
        stripped = cmd.lstrip()
        if stripped.upper().startswith("SELECT"):
            console.print(Syntax(cmd.rstrip(), "sql", word_wrap=True))
        else:
            console.print(f"  [dim]{cmd}[/dim]")
    console.print()


def _launch_sqlite3_repl(db_path: Path) -> None:
    """Run interactive ``sqlite3`` in this terminal (inherits stdin/stdout).

    Executes a short preview query (first 10 rows) before handing over to the REPL.
    """
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        console.print(f"[yellow]No database at {db_path}; skipping sqlite3[/yellow]")
        return
    sqlite3_exe = shutil.which("sqlite3")
    if not sqlite3_exe:
        console.print("[yellow]sqlite3 not found in PATH; skipping interactive shell[/yellow]")
        return
    console.print(f"[dim]Database[/dim]  [bold]{db_path}[/bold]")
    _print_sqlite3_startup_plan()
    console.print("[dim]Output from sqlite3 follows, then the interactive prompt. Type .quit to exit.[/dim]")
    argv: list[str] = [sqlite3_exe]
    for cmd in _SQLITE3_STARTUP_CMDS:
        argv.extend(("-cmd", cmd))
    argv.append(str(db_path))
    subprocess.run(
        argv,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=False,
    )


def _post_export_open_tools(csv_path: Path, db_path: Path | None = None) -> None:
    """Open ``master.csv`` with default app, then ``sqlite3`` on the DB in this shell."""
    csv_path = Path(csv_path).resolve()
    db = Path(db_path).resolve() if db_path else (csv_path.parent / "transactions.sqlite")
    _open_file_default_app(csv_path)
    _launch_sqlite3_repl(db)


def _sync_sqlite_from_master_csv(csv_path: Path) -> None:
    """Write ``transactions.sqlite`` next to ``master.csv`` (replaces table each run)."""
    csv_path = Path(csv_path).resolve()
    db_path = csv_path.parent / "transactions.sqlite"
    try:
        n = import_master_csv_to_sqlite(csv_path, db_path)
        console.print(f"[green]SQLite:[/green] {n} rows → {db_path}")
    except FileNotFoundError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except OSError as e:
        console.print(f"[yellow]SQLite export failed: {e}[/yellow]")


def _configure_logging() -> None:
    """Configure ccsa logger; use env CCSA_LOG=DEBUG for verbose output."""
    level_name = os.environ.get("CCSA_LOG", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log = logging.getLogger("ccsa")
    log.setLevel(level)
    if not log.handlers:
        h = RichHandler(
            console=console,
            show_time=True,
            omit_repeated_times=True,
            show_path=False,
            show_level=True,
            markup=True,
            rich_tracebacks=True,
            log_time_format="[%H:%M:%S]",
        )
        h.setLevel(level)
        log.addHandler(h)
    else:
        for h in log.handlers:
            h.setLevel(level)
    log.propagate = False


imap_app = typer.Typer(help="IMAP: fetch statement PDFs.")
app.add_typer(imap_app, name="imap")

pdf_app = typer.Typer(help="PDF: parse statement PDFs into normalized transactions.")
app.add_typer(pdf_app, name="pdf")

check_app = typer.Typer(help="Check data for gaps or issues.")
app.add_typer(check_app, name="check")

# Statement filenames typically start with YYYY-MM (e.g. 2025-08_208.pdf or 2025-07_15-07-2025.pdf)
_MONTH_PREFIX = re.compile(r"^(\d{4}-\d{2})")


def _month_from_stem(stem: str) -> str | None:
    """Return YYYY-MM from statement file stem, or None if not matched."""
    m = _MONTH_PREFIX.match(stem)
    return m.group(1) if m else None


def _months_in_range(start_ym: str, end_ym: str) -> list[str]:
    """Inclusive list of YYYY-MM from start_ym to end_ym."""
    y, mo = int(start_ym[:4]), int(start_ym[5:7])
    end_y, end_mo = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    while (y, mo) <= (end_y, end_mo):
        out.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo > 12:
            mo = 1
            y += 1
    return out


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    open_after: bool = typer.Option(
        False,
        "--open",
        "-O",
        help="After export: open master.csv with default app, then sqlite3 REPL in this terminal",
    ),
) -> None:
    """Fetch statements, normalize transactions, and export for analysis. Run full pipeline when no subcommand given."""
    if ctx.invoked_subcommand is not None:
        return
    _run_pipeline(open_after_export=open_after)


def _run_pipeline(
    force_normalize: bool = False,
    output_csv: Path | None = None,
    open_after_export: bool = False,
) -> None:
    """Full pipeline: setup, fetch, normalize, export. Re-runnable and self-healing."""
    _configure_logging()
    paths = get_paths()
    paths = ensure_local_dirs(paths)
    write_config_templates(paths)
    console.print("[dim]Setup: dirs and config ready[/dim]")

    loaded = load_config(paths)
    if loaded.config.email_rules:
        try:
            result = fetch_pdfs(paths)
            if result.downloaded:
                console.print(f"[green]Fetched {result.downloaded} new PDF(s)[/green]")
            elif result.skipped:
                console.print("[dim]Fetch: no new PDFs[/dim]")
        except Exception as e:
            console.print(f"[yellow]Fetch failed (continuing): {e}[/yellow]")
    else:
        console.print("[dim]Fetch: no email rules in card_rules.json, skipping[/dim]")

    skip_substrings = ["terms", "conditions", "most-important", "tariff", "mitc"]
    pdf_files = [p for p in paths.raw_pdfs_dir.rglob("*.pdf") if not any(s in p.stem.lower() for s in skip_substrings)]
    to_process = []
    for p in pdf_files:
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            if len(rel.parts) < 2:
                continue
            bank_slug, card_slug = rel.parts[0], rel.parts[1]
            if not get_parsers_for_bank(bank_slug):
                continue
            out_file = paths.normalized_dir / bank_slug / card_slug / f"{p.stem}.json"
            if out_file.exists() and not force_normalize:
                continue
            to_process.append((p, bank_slug, card_slug, out_file))
        except ValueError:
            continue

    if to_process:
        for p, bank_slug, card_slug, out_file in to_process:
            try:
                bank_name_str = bank_slug.title()
                card_name_str = card_slug.title()
                password = resolve_password(loaded, bank_name_str, card_name_str)
                raw = p.read_bytes()
                data, _ = _unlock_pdf(raw, password)
                text = pdf_module.extract_text_from_pdf(data)
                statement = try_parse_with_bank(
                    bank_slug, text, source_pdf_path=p,
                    bank_display=bank_name_str, card_display=card_name_str,
                )
                if statement is None:
                    console.print(f"[yellow]Skip (no parser): {p.relative_to(paths.repo_root)}[/yellow]")
                    continue
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
                console.print(f"[green]{len(statement.transactions)} txns[/green] [dim]{out_file.relative_to(paths.repo_root)}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Normalize failed for {p.name}: {e}[/yellow]")
    else:
        console.print("[dim]Normalize: nothing new to parse[/dim]")

    all_txns: list[Transaction] = []
    for jf in paths.normalized_dir.rglob("*.json"):
        try:
            st = Statement.model_validate_json(jf.read_text(encoding="utf-8"))
            for t in st.transactions:
                all_txns.append(t)
        except Exception as e:
            console.print(f"[yellow]Skip JSON {jf.name}: {e}[/yellow]")

    if not all_txns:
        console.print("[yellow]No transactions to export. Add PDFs or check card_rules.json.[/yellow]")
        return

    all_txns.sort(key=lambda t: (t.date, t.bank, t.card))
    out_path = output_csv or (paths.exports_dir / "master.csv")
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "bank", "card", "description", "amount", "currency", "category", "transaction_type", "tags"])
        for t in all_txns:
            tags = compute_tags(t.description, loaded.tags)
            writer.writerow([
                t.date, t.bank, t.card, t.description, t.amount, t.currency,
                t.category or "", t.transaction_type or "", tags,
            ])
    console.print(f"[green]All {len(all_txns)} transactions exported to {out_path}[/green]")
    _sync_sqlite_from_master_csv(out_path)
    if open_after_export:
        _post_export_open_tools(out_path)


@app.command()
def run(
    force: bool = typer.Option(False, "--force", "-f", help="Re-normalize all PDFs even if JSON exists"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Master CSV path (default: data/exports/master.csv)"),
    open_after: bool = typer.Option(
        False,
        "--open",
        "-O",
        help="After export: open master.csv with default app, then sqlite3 REPL in this terminal",
    ),
) -> None:
    """Run full pipeline: setup, fetch, normalize, export. Re-runnable and self-healing."""
    _run_pipeline(force_normalize=force, output_csv=output, open_after_export=open_after)


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
        "[dim]NL query:[/dim] [bold]pip install -e \".[llm]\"[/bold] then [bold]ccsa ollama setup[/bold] "
        "(starts Ollama if needed + pulls the model) — then [bold]ccsa query \"…\"[/bold]."
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
        raise SystemExit(1)


@pdf_app.command("parse")
def pdf_parse(
    pdf_path: Path = typer.Argument(..., help="Path to statement PDF (e.g. data/raw-pdfs/axis/neo/2025-03_....pdf)"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON here; default is stdout"),
    bank: str | None = typer.Option(None, help="Bank name (default: from path, e.g. axis -> Axis)"),
    card: str | None = typer.Option(None, help="Card name (default: from path, e.g. neo -> Neo)"),
) -> None:
    """Parse a statement PDF into normalized transactions (JSON). Uses parser for bank/card from path."""
    p = Path(pdf_path).resolve()
    if not p.exists():
        console.print(f"[red]File not found: {p}[/red]")
        raise SystemExit(1)
    paths = get_paths()
    loaded = load_config(paths)
    try:
        rel = p.relative_to(paths.raw_pdfs_dir)
        parts = rel.parts
        if len(parts) >= 2:
            bank_slug, card_slug = parts[0], parts[1]
            bank_name = bank or bank_slug.title()
            card_name = card or card_slug.title()
        else:
            bank_slug, card_slug = "", ""
            bank_name = bank or "Unknown"
            card_name = card or "Unknown"
    except ValueError:
        bank_slug, card_slug = "", ""
        bank_name = bank or "Unknown"
        card_name = card or "Unknown"
    parser = get_parser(bank_slug, card_slug)
    if parser is None:
        console.print(f"[red]No parser for {bank_name}/{card_name}. Supported banks: axis, hdfc, hsbc, icici, indusind, sbi[/red]")
        raise SystemExit(1)
    password = resolve_password(loaded, bank_name, card_name)
    raw = p.read_bytes()
    data, _ = _unlock_pdf(raw, password)
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
        raise SystemExit(1)
    out_json = statement.model_dump_json(indent=2)
    if output:
        Path(output).resolve().parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(out_json, encoding="utf-8")
        console.print(f"[green]Wrote {len(statement.transactions)} transactions to {output}[/green]")
    else:
        console.print(out_json)


@pdf_app.command("normalize")
def pdf_normalize(
    force: bool = typer.Option(False, "--force", "-f", help="Re-parse even if normalized JSON exists"),
) -> None:
    """Parse all statement PDFs in data/raw-pdfs/ and save normalized JSON to data/normalized/."""
    _configure_logging()
    paths = get_paths()
    ensure_local_dirs(paths)
    loaded = load_config(paths)
    pdf_files = list(paths.raw_pdfs_dir.rglob("*.pdf"))
    # Skip non-statement PDFs (e.g. terms and conditions)
    skip_substrings = ["terms", "conditions", "most-important", "tariff", "mitc"]
    to_process = []
    for p in pdf_files:
        stem_lower = p.stem.lower()
        if any(s in stem_lower for s in skip_substrings):
            continue
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            parts = rel.parts
            if len(parts) >= 2:
                bank_slug, card_slug = parts[0], parts[1]
            else:
                continue
        except ValueError:
            continue
        if not get_parsers_for_bank(bank_slug):
            console.print(f"[yellow]Skipping (no parser): {rel}[/yellow]")
            continue
        out_dir = paths.normalized_dir / bank_slug / card_slug
        out_file = out_dir / f"{p.stem}.json"
        if out_file.exists() and not force:
            continue
        to_process.append((p, bank_slug, card_slug, out_file))
    if not to_process:
        console.print("[dim]No PDFs to normalize (or all already done). Use --force to re-parse.[/dim]")
        return
    for p, bank_slug, card_slug, out_file in to_process:
        try:
            bank_name_str = bank_slug.title()
            card_name_str = card_slug.title()
            password = resolve_password(loaded, bank_name_str, card_name_str)
            raw = p.read_bytes()
            data, _ = _unlock_pdf(raw, password)
            text = pdf_module.extract_text_from_pdf(data)
            statement = try_parse_with_bank(
                bank_slug,
                text,
                source_pdf_path=p,
                bank_display=bank_name_str,
                card_display=card_name_str,
            )
            if statement is None:
                console.print(f"[red]{p.relative_to(paths.repo_root)}: no parser succeeded[/red]")
                continue
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
            console.print(f"[green]{len(statement.transactions)} txns[/green] [dim]{out_file.relative_to(paths.repo_root)}[/dim]")
        except Exception as e:
            console.print(f"[red]{p.relative_to(paths.repo_root)}: {e}[/red]")


@check_app.command("gaps")
def check_gaps(
    source: str = typer.Option("raw-pdfs", "--source", "-s", help="Where to look: raw-pdfs or normalized"),
) -> None:
    """Warn if any month is missing for a card between its first and last statement month."""
    paths = get_paths()
    skip_substrings = ["terms", "conditions", "most-important", "tariff", "mitc"]
    if source == "normalized":
        base = paths.normalized_dir
        files = list(base.rglob("*.json"))
    else:
        base = paths.raw_pdfs_dir
        files = [p for p in base.rglob("*.pdf") if not any(s in p.stem.lower() for s in skip_substrings)]

    # Group by (bank, card) -> set of YYYY-MM
    by_card: dict[tuple[str, str], set[str]] = {}
    for p in files:
        try:
            rel = p.relative_to(base)
            parts = rel.parts
            if len(parts) < 2:
                continue
            bank_slug, card_slug = parts[0], parts[1]
            ym = _month_from_stem(p.stem)
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
        expected = set(_months_in_range(start_ym, end_ym))
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
            console.print("[green]No gaps: every month is present between first and last statement for each card.[/green]")


export_app = typer.Typer(help="Export merged data.")
app.add_typer(export_app, name="export")


@export_app.command("master")
def export_master(
    format: str = typer.Option("csv", "--format", "-f", help="Output format: csv"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file (default: data/exports/master.csv)"),
    open_after: bool = typer.Option(
        False,
        "--open",
        "-O",
        help="After export: open master.csv with default app, then sqlite3 REPL in this terminal",
    ),
) -> None:
    """Merge all normalized statements into a single CSV. Parses PDFs if no normalized data exists."""
    paths = get_paths()
    ensure_local_dirs(paths)
    loaded = load_config(paths)
    all_txns: list[Transaction] = []
    json_files = list(paths.normalized_dir.rglob("*.json"))
    if json_files:
        for jf in json_files:
            try:
                st = Statement.model_validate_json(jf.read_text(encoding="utf-8"))
                for t in st.transactions:
                    all_txns.append(t)
            except Exception as e:
                console.print(f"[red]{jf}: {e}[/red]")
    if not all_txns:
        # Parse PDFs on the fly and merge
        console.print("[dim]No normalized JSON; parsing PDFs...[/dim]")
        skip_substrings = ["terms", "conditions", "most-important", "tariff", "mitc"]
        for p in paths.raw_pdfs_dir.rglob("*.pdf"):
            if any(s in p.stem.lower() for s in skip_substrings):
                continue
            try:
                rel = p.relative_to(paths.raw_pdfs_dir)
                parts = rel.parts
                if len(parts) < 2:
                    continue
                bank_slug, card_slug = parts[0], parts[1]
                parser = get_parser(bank_slug, card_slug)
                if parser is None:
                    continue
                bank_name_str = bank_slug.title()
                card_name_str = card_slug.title()
                password = resolve_password(loaded, bank_name_str, card_name_str)
                raw = p.read_bytes()
                data, _ = _unlock_pdf(raw, password)
                text = pdf_module.extract_text_from_pdf(data)
                statement = try_parse_with_bank(
                    bank_slug,
                    text,
                    source_pdf_path=str(p),
                    bank_display=bank_name_str,
                    card_display=card_name_str,
                )
                if statement is None:
                    continue
                for t in statement.transactions:
                    all_txns.append(t)
            except Exception as e:
                console.print(f"[red]{p}: {e}[/red]")
    if not all_txns:
        console.print("[yellow]No transactions found. Add PDFs to data/raw-pdfs/<bank>/<card>/ or run 'ccsa pdf normalize'.[/yellow]")
        raise SystemExit(1)
    all_txns.sort(key=lambda t: (t.date, t.bank, t.card))
    out_path = output or (paths.exports_dir / "master.csv")
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "bank", "card", "description", "amount", "currency", "category", "transaction_type", "tags"])
        for t in all_txns:
            tags = compute_tags(t.description, loaded.tags)
            writer.writerow([
                t.date,
                t.bank,
                t.card,
                t.description,
                t.amount,
                t.currency,
                t.category or "",
                t.transaction_type or "",
                tags,
            ])
    full_path = out_path.resolve()
    console.print(f"[green]All {len(all_txns)} transactions exported to {full_path}[/green]")
    _sync_sqlite_from_master_csv(out_path)
    if open_after:
        _post_export_open_tools(out_path)


@export_app.command("sqlite")
def export_sqlite(
    csv_path: Path | None = typer.Option(
        None,
        "--csv",
        "-c",
        help="Source master.csv (default: data/exports/master.csv)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="SQLite file (default: data/exports/transactions.sqlite)",
    ),
    open_after: bool = typer.Option(
        False,
        "--open",
        "-O",
        help="Open master.csv with default app, then sqlite3 REPL in this terminal",
    ),
) -> None:
    """Load master.csv into transactions.sqlite (replaces existing table)."""
    paths = get_paths()
    ensure_local_dirs(paths)
    csv_p = Path(csv_path or (paths.exports_dir / "master.csv")).resolve()
    db_p = Path(output or (csv_p.parent / "transactions.sqlite")).resolve()
    try:
        n = import_master_csv_to_sqlite(csv_p, db_p)
        console.print(f"[green]{n} rows written to {db_p}[/green]")
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    if open_after:
        _post_export_open_tools(csv_p, db_p)


ollama_app = typer.Typer(help="Ollama: local model server + weights for `ccsa query`.")
app.add_typer(ollama_app, name="ollama")


@ollama_app.command("setup")
def ollama_setup_command(
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Ollama model tag (default: CCSA_OLLAMA_MODEL or qwen3.5:0.8b-q8_0)",
    ),
) -> None:
    """Start ``ollama serve`` in the background if needed, then ``ollama pull`` the model."""
    from .llm_query import DEFAULT_OLLAMA_MODEL
    from .ollama_setup import (
        ensure_ollama_api_and_tags,
        model_in_tags_payload,
        normalize_base_url,
        pull_ollama_model_if_needed,
    )

    paths = get_paths()
    ensure_local_dirs(paths)
    m = model or os.environ.get("CCSA_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    base = normalize_base_url(os.environ.get("CCSA_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))

    try:
        with console.status("[bold green]Checking Ollama…"):
            tags, messages, started = ensure_ollama_api_and_tags(
                base,
                paths=paths,
                start_background=True,
            )
        if not model_in_tags_payload(tags, m):
            console.print(f"[dim]Pulling model {m!r} (this may take a while)…[/dim]")
        messages.extend(
            pull_ollama_model_if_needed(tags, m, pull_if_missing=True, announce_pull=False)
        )
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        console.print(
            "[dim]Install Ollama: https://ollama.com/download — then re-run "
            "[bold]ccsa ollama setup[/bold][/dim]"
        )
        raise SystemExit(1)

    for line in messages:
        okish = (
            "Ready" in line
            or "reachable" in line.lower()
            or "Started" in line
            or "already present" in line.lower()
        )
        console.print(f"[green]{line}[/green]" if okish else f"[dim]{line}[/dim]")
    if started:
        console.print(f"[dim]Logs: {paths.local_state_dir / 'ollama_serve.log'}[/dim]")


@app.command("query")
def query_command(
    question: str = typer.Argument(..., help="Natural-language question over transactions"),
    db_path: Path | None = typer.Option(
        None,
        "--db",
        "-d",
        help="SQLite database (default: data/exports/transactions.sqlite)",
    ),
    sql_only: bool = typer.Option(
        False,
        "--sql-only",
        help="Only run planner + validation; print SQL, do not execute or call answer model",
    ),
    sample_rows: int = typer.Option(
        20,
        "--sample-rows",
        help="Random rows included in planner context",
        min=1,
        max=500,
    ),
    max_iterations: int = typer.Option(
        5,
        "--max-iterations",
        "-n",
        help="Max planner turns (SQL / clarify / answer loop); then synthesis if needed",
        min=1,
        max=20,
    ),
    verbose_steps: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print each SQL step in the loop",
    ),
    ensure_server: bool = typer.Option(
        True,
        "--ensure-server/--no-ensure-server",
        help="If Ollama is down, start `ollama serve` in the background and pull the model if missing",
    ),
) -> None:
    """Ask a question in English using a local Ollama model (requires: ``pip install -e '.[llm]'``)."""
    try:
        import langchain_core  # noqa: F401
        import langchain_ollama  # noqa: F401
        from .llm_query import DEFAULT_OLLAMA_BASE_URL, DEFAULT_OLLAMA_MODEL, run_natural_language_query
        from .ollama_setup import (
            ensure_ollama_api_and_tags,
            model_in_tags_payload,
            normalize_base_url,
            pull_ollama_model_if_needed,
        )
    except ImportError as e:
        console.print(
            "[red]Missing LLM dependencies. Install:[/red] [bold]pip install -e \".[llm]\"[/bold]\n"
            f"[dim]{e}[/dim]"
        )
        raise SystemExit(1)

    paths = get_paths()
    ensure_local_dirs(paths)
    db = Path(db_path or (paths.exports_dir / "transactions.sqlite")).resolve()
    if not db.is_file():
        console.print(f"[red]Database not found: {db}[/red] [dim](run `ccsa export master` or `ccsa export sqlite`)[/dim]")
        raise SystemExit(1)

    if ensure_server:
        try:
            base_ollama = normalize_base_url(DEFAULT_OLLAMA_BASE_URL)
            with console.status("[bold green]Checking Ollama…"):
                tags, msgs, _ = ensure_ollama_api_and_tags(
                    base_ollama,
                    paths=paths,
                    start_background=True,
                )
            if not model_in_tags_payload(tags, DEFAULT_OLLAMA_MODEL):
                console.print(
                    f"[dim]Pulling model {DEFAULT_OLLAMA_MODEL!r} (this may take a while)…[/dim]"
                )
            msgs.extend(
                pull_ollama_model_if_needed(
                    tags,
                    DEFAULT_OLLAMA_MODEL,
                    pull_if_missing=True,
                    announce_pull=False,
                )
            )
            for line in msgs:
                console.print(f"[dim]{line}[/dim]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            console.print("[dim]Tip: [bold]ccsa ollama setup[/bold] or install https://ollama.com/download[/dim]")
            raise SystemExit(1)

    console.print(
        f"[dim]Ollama[/dim] [bold]{DEFAULT_OLLAMA_MODEL}[/bold] @ [dim]{DEFAULT_OLLAMA_BASE_URL}[/]"
        f" [dim]· max {max_iterations} iteration(s)[/dim]"
    )
    with console.status("[bold green]Query: starting…[/bold green]", spinner="dots") as query_status:

        def _query_progress(line: str) -> None:
            query_status.update(f"[bold green]{rich_escape(line)}[/bold green]")

        result = run_natural_language_query(
            question,
            str(db),
            sample_rows=sample_rows,
            sql_only=sql_only,
            max_iterations=max_iterations,
            progress_callback=_query_progress,
        )

    if result.clarification:
        console.print("[yellow]Need clarification:[/yellow]")
        console.print(result.clarification)
        raise SystemExit(0)

    if verbose_steps and result.steps:
        console.print("[dim]── Loop steps ──[/dim]")
        for st in result.steps:
            console.print(f"  [cyan]Step {st.iteration}[/cyan] [dim]{st.sql[:120]}{'…' if len(st.sql) > 120 else ''}[/dim]")
            if st.error:
                console.print(f"    [red]{st.error}[/red]")
            else:
                console.print(f"    [dim]→ {st.row_count} row(s)[/dim]")
        if result.stopped_reason:
            console.print(f"[dim]Stopped: {result.stopped_reason}[/dim]")
        console.print()

    # Planner parse / validation failure before any SQL
    if result.error and not result.sql_executed:
        console.print(f"[red]{result.error}[/red]")
        if result.planner_raw:
            console.print("[dim]Last planner output (raw):[/dim]")
            pr = result.planner_raw[:8000]
            try:
                console.print(Syntax(pr, "json", word_wrap=True))
            except Exception:
                console.print(pr)
        raise SystemExit(1)

    if result.sql_executed:
        console.print("[dim]SQL:[/dim]")
        console.print(Syntax(result.sql_executed, "sql", word_wrap=True))

    if sql_only:
        console.print("[dim]--sql-only:[/dim] skipping execute and answer.")
        raise SystemExit(0)

    if result.rows:
        tbl = Table(show_header=True, header_style="bold", border_style="dim")
        for col in result.rows[0].keys():
            tbl.add_column(str(col), overflow="fold")
        for row in result.rows[:50]:
            tbl.add_row(*[str(row.get(c, "")) for c in result.rows[0].keys()])
        if len(result.rows) > 50:
            console.print(f"[dim](showing 50 of {len(result.rows)} rows)[/dim]")
        console.print(tbl)
        console.print()

    if result.error:
        console.print(f"[red]{result.error}[/red]")
        raise SystemExit(1)

    console.print(result.answer)


if __name__ == "__main__":
    app()

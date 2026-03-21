from __future__ import annotations

import logging
from pathlib import Path
import re

import typer
from rich.console import Console

from .config import compute_tags, ensure_local_dirs, load_config, resolve_password, write_config_templates
from .paths import get_paths
from .imap import fetch_pdfs, _unlock_pdf
from . import pdf as pdf_module
from .parsers import get_parser, get_parsers_for_bank, try_parse_with_bank
from .parsers.schema import Statement

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
def main(ctx: typer.Context) -> None:
    """Fetch statements, normalize transactions, and export for analysis. Run full pipeline when no subcommand given."""
    if ctx.invoked_subcommand is not None:
        return
    _run_pipeline()


def _run_pipeline(
    force_normalize: bool = False,
    output_csv: Path | None = None,
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

    all_txns: list[tuple] = []
    for jf in paths.normalized_dir.rglob("*.json"):
        try:
            st = Statement.model_validate_json(jf.read_text(encoding="utf-8"))
            src = st.source_pdf_path or str(jf)
            for t in st.transactions:
                all_txns.append((t, src))
        except Exception as e:
            console.print(f"[yellow]Skip JSON {jf.name}: {e}[/yellow]")

    if not all_txns:
        console.print("[yellow]No transactions to export. Add PDFs or check card_rules.json.[/yellow]")
        return

    all_txns.sort(key=lambda x: (x[0].date, x[0].bank, x[0].card))
    out_path = output_csv or (paths.exports_dir / "master.csv")
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "bank", "card", "description", "amount", "currency", "category", "transaction_type", "tags", "source"])
        for t, src in all_txns:
            tags = compute_tags(t.description, loaded.tags)
            writer.writerow([
                t.date, t.bank, t.card, t.description, t.amount, t.currency,
                t.category or "", t.transaction_type or "", tags, src,
            ])
    console.print(f"[green]All {len(all_txns)} transactions exported to {out_path}[/green]")


@app.command()
def run(
    force: bool = typer.Option(False, "--force", "-f", help="Re-normalize all PDFs even if JSON exists"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Master CSV path (default: data/exports/master.csv)"),
) -> None:
    """Run full pipeline: setup, fetch, normalize, export. Re-runnable and self-healing."""
    _run_pipeline(force_normalize=force, output_csv=output)


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
) -> None:
    """Merge all normalized statements into a single CSV. Parses PDFs if no normalized data exists."""
    paths = get_paths()
    ensure_local_dirs(paths)
    loaded = load_config(paths)
    all_txns = []
    json_files = list(paths.normalized_dir.rglob("*.json"))
    if json_files:
        for jf in json_files:
            try:
                st = Statement.model_validate_json(jf.read_text(encoding="utf-8"))
                source = st.source_pdf_path or str(jf)
                for t in st.transactions:
                    all_txns.append((t, source))
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
                source = statement.source_pdf_path or str(p)
                for t in statement.transactions:
                    all_txns.append((t, source))
            except Exception as e:
                console.print(f"[red]{p}: {e}[/red]")
    if not all_txns:
        console.print("[yellow]No transactions found. Add PDFs to data/raw-pdfs/<bank>/<card>/ or run 'ccsa pdf normalize'.[/yellow]")
        raise SystemExit(1)
    all_txns.sort(key=lambda x: (x[0].date, x[0].bank, x[0].card))
    out_path = output or (paths.exports_dir / "master.csv")
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "bank", "card", "description", "amount", "currency", "category", "transaction_type", "tags", "source"])
        for t, source in all_txns:
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
                source,
            ])
    full_path = out_path.resolve()
    console.print(f"[green]All {len(all_txns)} transactions exported to {full_path}[/green]")


if __name__ == "__main__":
    app()

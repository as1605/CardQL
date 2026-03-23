from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

from ..config import compute_tags, ensure_local_dirs, load_config, resolve_password, write_config_templates
from ..export import import_master_csv_to_sqlite
from ..ingest import fetch_pdfs, pdf as pdf_module, unlock_pdf
from ..parsers import get_parser, get_parsers_for_bank, try_parse_with_bank
from ..parsers.schema import Statement, Transaction
from ..paths import Paths, get_paths
from .helpers import console, open_file_default_app


SKIP_PDF_SUBSTRINGS = ("terms", "conditions", "most-important", "tariff", "mitc")


def sync_sqlite_from_master_csv(csv_path: Path) -> None:
    csv_path = Path(csv_path).resolve()
    db_path = csv_path.parent / "transactions.sqlite"
    try:
        n = import_master_csv_to_sqlite(csv_path, db_path)
        console.print(f"[green]SQLite:[/green] {n} rows → {db_path}")
    except FileNotFoundError as e:
        console.print(f"[yellow]{e}[/yellow]")
    except OSError as e:
        console.print(f"[yellow]SQLite export failed: {e}[/yellow]")


def merge_normalized_to_csv(paths: Paths, loaded) -> Path | None:
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
        return None

    all_txns.sort(key=lambda t: (t.date, t.bank, t.card))
    out_path = (paths.exports_dir / "master.csv").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["date", "bank", "card", "description", "amount", "currency", "category", "transaction_type", "tags"]
        )
        for t in all_txns:
            tags = compute_tags(t.description, loaded.tags)
            writer.writerow(
                [
                    t.date,
                    t.bank,
                    t.card,
                    t.description,
                    t.amount,
                    t.currency,
                    t.category or "",
                    t.transaction_type or "",
                    tags,
                ]
            )
    console.print(f"[green]All {len(all_txns)} transactions exported to {out_path}[/green]")
    return out_path


def normalize_pdfs(
    paths: Paths,
    loaded,
    *,
    force_normalize: bool = False,
    single_pdf: Path | None = None,
) -> None:
    if single_pdf is not None:
        p = Path(single_pdf).resolve()
        if not p.exists():
            console.print(f"[red]File not found: {p}[/red]")
            return
        try:
            rel = p.relative_to(paths.raw_pdfs_dir)
            parts = rel.parts
            if len(parts) >= 2:
                bank_slug, card_slug = parts[0], parts[1]
            else:
                bank_slug, card_slug = "", ""
        except ValueError:
            bank_slug, card_slug = "", ""
        if not bank_slug or not get_parsers_for_bank(bank_slug):
            console.print("[red]PDF must be under data/raw-pdfs/<bank>/<card>/[/red]")
            return
        out_file = paths.normalized_dir / bank_slug / card_slug / f"{p.stem}.json"
        if out_file.exists() and not force_normalize:
            console.print(f"[dim]Already normalized: {out_file}[/dim]")
            return
        bank_name_str = bank_slug.title()
        card_name_str = card_slug.title()
        password = resolve_password(loaded, bank_name_str, card_name_str)
        raw = p.read_bytes()
        data, _ = unlock_pdf(raw, password)
        text = pdf_module.extract_text_from_pdf(data)
        statement = try_parse_with_bank(
            bank_slug,
            text,
            source_pdf_path=p,
            bank_display=bank_name_str,
            card_display=card_name_str,
        )
        if statement is None:
            console.print(f"[red]No parser succeeded for {p}[/red]")
            return
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]{len(statement.transactions)} txns[/green] [dim]{out_file}[/dim]")
        return

    pdf_files = [
        p
        for p in paths.raw_pdfs_dir.rglob("*.pdf")
        if not any(s in p.stem.lower() for s in SKIP_PDF_SUBSTRINGS)
    ]
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

    if not to_process:
        console.print("[dim]Normalize: nothing new to parse[/dim]")
        return

    for p, bank_slug, card_slug, out_file in to_process:
        try:
            bank_name_str = bank_slug.title()
            card_name_str = card_slug.title()
            password = resolve_password(loaded, bank_name_str, card_name_str)
            raw = p.read_bytes()
            data, _ = unlock_pdf(raw, password)
            text = pdf_module.extract_text_from_pdf(data)
            statement = try_parse_with_bank(
                bank_slug,
                text,
                source_pdf_path=p,
                bank_display=bank_name_str,
                card_display=card_name_str,
            )
            if statement is None:
                console.print(f"[yellow]Skip (no parser): {p.relative_to(paths.repo_root)}[/yellow]")
                continue
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(statement.model_dump_json(indent=2), encoding="utf-8")
            console.print(
                f"[green]{len(statement.transactions)} txns[/green] [dim]{out_file.relative_to(paths.repo_root)}[/dim]"
            )
        except Exception as e:
            console.print(f"[yellow]Normalize failed for {p.name}: {e}[/yellow]")


def run_data_build(
    *,
    force_normalize: bool = False,
    output_csv: Path | None = None,
    open_csv: bool = True,
    single_pdf: Path | None = None,
    skip_fetch: bool = False,
    configure_log: bool = True,
) -> Path | None:
    """Init dirs, optional fetch, normalize PDFs, export CSV + SQLite, optionally open CSV."""
    if configure_log:
        from .helpers import configure_logging

        configure_logging()

    paths = get_paths()
    paths = ensure_local_dirs(paths)
    write_config_templates(paths)
    console.print("[dim]Setup: dirs and config ready[/dim]")

    loaded = load_config(paths)
    if not skip_fetch and loaded.config.email_rules:
        try:
            result = fetch_pdfs(paths)
            if result.downloaded:
                console.print(f"[green]Fetched {result.downloaded} new PDF(s)[/green]")
            elif result.skipped:
                console.print("[dim]Fetch: no new PDFs[/dim]")
        except Exception as e:
            console.print(f"[yellow]Fetch failed (continuing): {e}[/yellow]")
    elif not skip_fetch:
        console.print("[dim]Fetch: no email rules in card_rules.json, skipping[/dim]")

    normalize_pdfs(paths, loaded, force_normalize=force_normalize, single_pdf=single_pdf)

    out_path = merge_normalized_to_csv(paths, loaded)
    if out_path is None:
        return None
    final = Path(output_csv).resolve() if output_csv else out_path
    if output_csv and final != out_path:
        final.parent.mkdir(parents=True, exist_ok=True)
        import shutil as sh

        sh.copy2(out_path, final)
        out_path = final

    sync_sqlite_from_master_csv(out_path)
    if open_csv:
        open_file_default_app(out_path)
    return out_path


def run_ollama_setup(model: str | None = None) -> bool:
    from ..query import DEFAULT_OLLAMA_MODEL
    from ..query.ollama_setup import (
        ensure_ollama_api_and_tags,
        model_in_tags_payload,
        normalize_base_url,
        pull_ollama_model_if_needed,
    )

    paths = get_paths()
    ensure_local_dirs(paths)
    m = model or os.environ.get("CARDQL_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    base = normalize_base_url(os.environ.get("CARDQL_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))

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
            "[bold]cardql ollama[/bold][/dim]"
        )
        return False

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
    return True


def launch_streamlit(port: int, host: str) -> None:
    from .helpers import streamlit_app_path

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


def run_full_stack(
    *,
    force_normalize: bool = False,
    output_csv: Path | None = None,
    open_csv: bool = True,
    no_fetch: bool = False,
    skip_ollama: bool = False,
    no_ui: bool = False,
) -> None:
    """Bare `cardql`: init → fetch → data build → ollama → streamlit."""
    from .helpers import configure_logging

    configure_logging()
    paths = get_paths()
    paths = ensure_local_dirs(paths)
    write_config_templates(paths)
    console.print("[dim]Setup: dirs and config ready[/dim]")

    loaded = load_config(paths)
    if not no_fetch and loaded.config.email_rules:
        try:
            result = fetch_pdfs(paths)
            if result.downloaded:
                console.print(f"[green]Fetched {result.downloaded} new PDF(s)[/green]")
            elif result.skipped:
                console.print("[dim]Fetch: no new PDFs[/dim]")
        except Exception as e:
            console.print(f"[yellow]Fetch failed (continuing): {e}[/yellow]")
    elif not no_fetch:
        console.print("[dim]Fetch: no email rules in card_rules.json, skipping[/dim]")

    normalize_pdfs(paths, loaded, force_normalize=force_normalize, single_pdf=None)

    out_path = merge_normalized_to_csv(paths, loaded)
    if out_path is None:
        console.print("[yellow]No transactions to export yet.[/yellow]")
    else:
        final = Path(output_csv).resolve() if output_csv else out_path
        if output_csv and final != out_path:
            final.parent.mkdir(parents=True, exist_ok=True)
            import shutil as sh

            sh.copy2(out_path, final)
            out_path = final
        sync_sqlite_from_master_csv(out_path)
        if open_csv:
            open_file_default_app(out_path)

    if not skip_ollama:
        run_ollama_setup()
    if not no_ui:
        launch_streamlit(8501, "127.0.0.1")

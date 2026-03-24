from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import ensure_local_dirs, load_config, write_config_templates
from ..export import merge_normalized_to_master_csv, sync_master_csv_to_sqlite
from ..ingest import fetch_pdfs, normalize_pdfs
from ..paths import get_paths
from .helpers import console, open_file_default_app


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

    out_path = merge_normalized_to_master_csv(paths, loaded.tags)
    if out_path is None:
        return None
    final = Path(output_csv).resolve() if output_csv else out_path
    if output_csv and final != out_path:
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, final)
        out_path = final

    sync_master_csv_to_sqlite(out_path)
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

    out_path = merge_normalized_to_master_csv(paths, loaded.tags)
    if out_path is not None:
        final = Path(output_csv).resolve() if output_csv else out_path
        if output_csv and final != out_path:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_path, final)
            out_path = final
        sync_master_csv_to_sqlite(out_path)
        if open_csv:
            open_file_default_app(out_path)

    if not skip_ollama:
        run_ollama_setup()
    if not no_ui:
        launch_streamlit(8501, "127.0.0.1")

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.syntax import Syntax

console = Console()

# Statement filenames typically start with YYYY-MM
_MONTH_PREFIX = re.compile(r"^(\d{4}-\d{2})")

_SQLITE3_STARTUP_CMDS: tuple[str, ...] = (
    ".headers on",
    ".mode table",
    "SELECT date,bank,card,description,amount,tags FROM transactions LIMIT 10;",
)


def month_from_stem(stem: str) -> str | None:
    m = _MONTH_PREFIX.match(stem)
    return m.group(1) if m else None


def months_in_range(start_ym: str, end_ym: str) -> list[str]:
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


def open_file_default_app(path: Path) -> None:
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


def print_sqlite3_startup_plan() -> None:
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


def launch_sqlite3_repl(db_path: Path) -> None:
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        console.print(f"[yellow]No database at {db_path}; skipping sqlite3[/yellow]")
        return
    sqlite3_exe = shutil.which("sqlite3")
    if not sqlite3_exe:
        console.print("[yellow]sqlite3 not found in PATH; skipping interactive shell[/yellow]")
        return
    console.print(f"[dim]Database[/dim]  [bold]{db_path}[/bold]")
    print_sqlite3_startup_plan()
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


def configure_logging() -> None:
    level_name = os.environ.get("CARDQL_LOG", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log = logging.getLogger("cardql")
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


def streamlit_app_path() -> Path:
    import cardql.ui

    return Path(cardql.ui.__file__).resolve().parent / "streamlit_app.py"

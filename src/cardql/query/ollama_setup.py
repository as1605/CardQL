"""
Start the Ollama API (``ollama serve``) in the background if needed and pull the chat model.

Requires the **ollama** CLI on PATH. Install from https://ollama.com/download
(desktop app or CLI) as you prefer.

Uses only the standard library: one ``GET /api/tags`` per check (server up + model list in a single response when the API is reachable).
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Any, Callable, cast

from ..paths import Paths, get_paths

log = logging.getLogger("cardql.ollama_setup")

PID_FILENAME = "ollama_serve_cardql.pid"
LOG_FILENAME = "ollama_serve.log"

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"


def ollama_install_hint_one_line() -> str:
    """Short message for errors and logs."""
    return f"Install from {OLLAMA_DOWNLOAD_URL} (desktop app or CLI on PATH)."


def ollama_binary() -> str | None:
    return shutil.which("ollama")


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/") or "http://127.0.0.1:11434"


def fetch_api_tags(base_url: str, timeout_s: float = 10.0) -> dict[str, Any] | None:
    """
    ``GET /api/tags`` — returns parsed JSON or ``None`` if the server is down / error.
    One round-trip yields both liveness and the model list.
    """
    base = normalize_base_url(base_url)
    try:
        req = urllib.request.Request(
            f"{base}/api/tags",
            method="GET",
            headers={"User-Agent": "cardql/ollama-setup"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        log.debug("fetch_api_tags failed: %s", e)
        return None


def is_server_up(base_url: str, timeout_s: float = 2.0) -> bool:
    """Return True if Ollama responds on ``GET /api/tags``."""
    return fetch_api_tags(base_url, timeout_s=timeout_s) is not None


def model_in_tags_payload(data: dict[str, Any], model: str) -> bool:
    """Return True if ``model`` appears in a ``/api/tags`` response body."""
    models = data.get("models") or []
    for m in models:
        name = (m.get("name") or "").strip()
        if name == model or name.startswith(model + ":"):
            return True
    return False


def model_is_present(base_url: str, model: str) -> bool:
    """Return True if ``model`` appears in ``/api/tags`` (exact name or tag prefix)."""
    data = fetch_api_tags(base_url, timeout_s=10.0)
    if data is None:
        return False
    return model_in_tags_payload(data, model)


def wait_for_server(base_url: str, *, timeout_s: float = 90.0, poll_s: float = 0.4) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_server_up(base_url):
            return True
        time.sleep(poll_s)
    return False


def _popen_ollama_serve(log_fp: IO[str]) -> subprocess.Popen[Any]:
    """Start ``ollama serve`` detached; logs to ``log_fp``."""
    ollama = ollama_binary()
    if not ollama:
        raise RuntimeError(
            "ollama executable not found on PATH. " + ollama_install_hint_one_line()
        )
    kw: dict[str, Any] = {
        "stdout": log_fp,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    return cast(
        subprocess.Popen[Any],
        subprocess.Popen([ollama, "serve"], **kw),
    )


def _read_pid(paths: Paths) -> int | None:
    p = paths.local_state_dir / PID_FILENAME
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(paths: Paths, pid: int) -> None:
    paths.local_state_dir.mkdir(parents=True, exist_ok=True)
    (paths.local_state_dir / PID_FILENAME).write_text(str(pid) + "\n", encoding="utf-8")


def start_ollama_serve_background(paths: Paths | None = None) -> tuple[bool, str]:
    """
    If the API is already up, return (False, message).
    Otherwise start ``ollama serve`` in the background and return (True, message).

    Writes PID to ``.local/state/ollama_serve_cardql.pid`` and logs to ``ollama_serve.log``.
    """
    paths = paths or get_paths()
    base_url = normalize_base_url(os.environ.get("CARDQL_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))

    if is_server_up(base_url):
        return False, "Ollama API already running."

    if not ollama_binary():
        raise RuntimeError(
            "Ollama is not installed or not on PATH. " + ollama_install_hint_one_line()
        )

    paths.local_state_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.local_state_dir / LOG_FILENAME
    log_f = open(log_path, "a", encoding="utf-8")
    log_f.write(f"\n--- cardql ollama serve {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_f.flush()

    proc = _popen_ollama_serve(log_f)
    _write_pid(paths, proc.pid)
    log.info("Started ollama serve pid=%s log=%s", proc.pid, log_path)

    if not wait_for_server(base_url, timeout_s=90.0):
        log_f.close()
        raise RuntimeError(
            f"Ollama did not become ready at {base_url} within 90s. See {log_path}"
        )

    return True, f"Started Ollama in the background (pid {proc.pid}). Log: {log_path}"


def pull_model(model: str, *, capture_output: bool = False) -> int:
    """
    Run ``ollama pull <model>``. Returns process exit code.
    By default streams output to the parent's stdout/stderr (download progress).
    """
    ollama = ollama_binary()
    if not ollama:
        raise RuntimeError(
            "ollama executable not found on PATH. " + ollama_install_hint_one_line()
        )
    if capture_output:
        r = subprocess.run(
            [ollama, "pull", model],
            check=False,
            capture_output=True,
            text=True,
        )
        return r.returncode
    return subprocess.run([ollama, "pull", model], check=False).returncode


def pull_model_api_stream(
    base_url: str,
    model: str,
    *,
    progress_callback: Callable[[float], None] | None = None,
) -> None:
    """
    Pull a model via ``POST /api/pull`` with ``stream: true`` and parse NDJSON lines.

    Invokes *progress_callback* with a fraction in ``[0, 1]`` when ``completed`` / ``total``
    appear (per-layer download progress). Raises on ``error`` in the stream or HTTP failure.
    """
    url = f"{normalize_base_url(base_url)}/api/pull"
    body = json.dumps({"name": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "cardql/ollama-setup"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=None)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            pass
        raise RuntimeError(f"Ollama pull API HTTP {e.code}: {e.reason} {detail}".strip()) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama pull API unreachable: {e}") from e

    last_frac = 0.0
    with resp:
        tw = io.TextIOWrapper(resp, encoding="utf-8")
        for line in tw:
            line = line.strip()
            if not line:
                continue
            try:
                obj: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                log.debug("ollama pull non-JSON line: %s", line[:120])
                continue
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            status = str(obj.get("status") or "")
            if status.lower() == "success" and progress_callback:
                last_frac = 1.0
                progress_callback(1.0)
                continue
            tot = obj.get("total")
            completed = obj.get("completed")
            if (
                isinstance(tot, (int, float))
                and float(tot) > 0
                and isinstance(completed, (int, float))
                and progress_callback
            ):
                frac = min(1.0, max(0.0, float(completed) / float(tot)))
                last_frac = max(last_frac, frac)
                progress_callback(last_frac)

    if progress_callback and last_frac < 1.0:
        progress_callback(1.0)


def ensure_ollama_api_and_tags(
    base_url: str,
    *,
    paths: Paths | None = None,
    start_background: bool = True,
) -> tuple[dict[str, Any], list[str], bool]:
    """
    Reach the Ollama API and return the ``/api/tags`` JSON.

    Does **not** run ``ollama pull`` — call :func:`pull_ollama_model_if_needed` after this so
    Rich/spinner UIs can end their live display before subprocess progress hits the terminal
    (avoids flicker between ``console.status()`` and ``ollama pull`` output).

    Returns ``(tags, messages, server_started)``.
    """
    paths = paths or get_paths()
    base_url = normalize_base_url(base_url)
    messages: list[str] = []
    started = False

    tags = fetch_api_tags(base_url, timeout_s=10.0)

    if tags is None:
        if not start_background:
            raise RuntimeError(
                f"Ollama API not reachable at {base_url}. "
                "Start Ollama (desktop app or `ollama serve`) or run: cardql ollama"
            )
        did, msg = start_ollama_serve_background(paths)
        messages.append(msg)
        if did:
            started = True
        tags = fetch_api_tags(base_url, timeout_s=10.0)
        if tags is None:
            raise RuntimeError(
                f"Ollama API still not reachable at {base_url} after starting server. "
                f"See {paths.local_state_dir / LOG_FILENAME}"
            )
    else:
        messages.append("Ollama API is reachable.")

    return tags, messages, started


def pull_ollama_model_if_needed(
    tags: dict[str, Any],
    model: str,
    *,
    pull_if_missing: bool = True,
    announce_pull: bool = True,
    base_url: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> list[str]:
    """
    If ``pull_if_missing`` and the model is absent from ``tags``, run ``ollama pull``.

    Streamed pull output should run **outside** Rich live/status widgets to avoid flicker.

    When ``announce_pull`` is True (default), prints a short line to stderr before pulling
    (for programmatic use of :func:`ensure_ollama_ready`). UIs that print their own line
    should pass ``announce_pull=False``.

    When *progress_callback* is set, uses ``POST /api/pull`` streaming (needs *base_url* or
    ``CARDQL_OLLAMA_BASE_URL``) so callers can show download progress; otherwise uses the CLI.
    """
    if not pull_if_missing:
        return []
    if not model_in_tags_payload(tags, model):
        if announce_pull:
            print(
                f"Pulling model {model!r} (this may take a while)…",
                file=sys.stderr,
                flush=True,
            )
        if progress_callback is not None:
            bu = normalize_base_url(
                base_url or os.environ.get("CARDQL_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            )
            pull_model_api_stream(bu, model, progress_callback=progress_callback)
        else:
            code = pull_model(model, capture_output=False)
            if code != 0:
                raise RuntimeError(f"ollama pull {model!r} failed with exit code {code}")
        return [f"Model {model!r} ready."]
    return [f"Model {model!r} already present."]


def ensure_ollama_ready(
    base_url: str,
    model: str,
    *,
    paths: Paths | None = None,
    start_background: bool = True,
    pull_if_missing: bool = True,
) -> tuple[list[str], bool]:
    """
    Ensure API is reachable and (optionally) the model is pulled.

    Uses **one** ``GET /api/tags`` when the server is already up to decide both liveness and
    whether ``pull`` is needed.

    Returns ``(messages, server_started)`` where ``server_started`` is True if this call
    started ``ollama serve``.

    For terminal UIs, prefer :func:`ensure_ollama_api_and_tags` then
    :func:`pull_ollama_model_if_needed` so the spinner can stop before ``ollama pull``.
    """
    tags, messages, started = ensure_ollama_api_and_tags(
        base_url,
        paths=paths,
        start_background=start_background,
    )
    messages.extend(
        pull_ollama_model_if_needed(tags, model, pull_if_missing=pull_if_missing, announce_pull=True)
    )
    return messages, started

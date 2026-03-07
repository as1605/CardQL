from __future__ import annotations

import imaplib
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock

import pikepdf

from .config import (
    BankEmailRule,
    LoadedConfig,
    get_imap_credentials,
    get_imap_passwords_for_inbox,
    get_inbox_emails,
    load_config,
    resolve_password,
)
from .paths import Paths, get_paths

log = logging.getLogger("ccsa.imap")

STATE_FILE = "imap_fetched.json"
FETCH_WORKERS = 5


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DownloadRecord:
    uid: str
    bank: str
    card: str
    filename: str
    saved_path: str
    fetched_at: str
    unlocked: bool = False


@dataclass
class RuleSummary:
    bank: str
    card: str
    found: int
    skipped: int
    downloaded: int
    reunlocked: int = 0


@dataclass
class FetchResult:
    downloaded: int
    skipped: int
    reunlocked: int
    folder: str
    saved_paths: list[str]
    rule_summaries: list[RuleSummary]


# ---------------------------------------------------------------------------
# State: keyed by uid (str)
# ---------------------------------------------------------------------------

def _state_path(paths: Paths) -> Path:
    return paths.local_state_dir / STATE_FILE


def _load_state(paths: Paths) -> dict[str, DownloadRecord]:
    """Load state from disk. Handles both old (downloaded) and new (records) format."""
    p = _state_path(paths)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read state file: %s", e)
        return {}

    # Support old format {"downloaded": [...]} and new format {"records": [...]}
    items = data.get("records") or data.get("downloaded") or []
    state: dict[str, DownloadRecord] = {}
    for item in items:
        uid = str(item.get("uid", "")).strip()
        if not uid:
            continue
        state[uid] = DownloadRecord(
            uid=uid,
            bank=item.get("bank", ""),
            card=item.get("card", ""),
            filename=item.get("filename", ""),
            saved_path=item.get("saved_path", ""),
            fetched_at=item.get("fetched_at", ""),
            unlocked=bool(item.get("unlocked", False)),
        )
    return state


def _save_state(paths: Paths, state: dict[str, DownloadRecord]) -> None:
    records = [asdict(r) for r in sorted(state.values(), key=lambda r: r.uid)]
    _state_path(paths).write_text(
        json.dumps({"records": records}, indent=2) + "\n", encoding="utf-8"
    )


def _reconcile_state_with_disk(
    paths: Paths,
    state: dict[str, DownloadRecord],
) -> int:
    """
    Scan the data directory and update state to match disk:
    - Remove any record whose saved_path is set but the file no longer exists.
    - Saves updated state so the JSON file stays in sync.
    Returns number of entries removed.
    """
    removed = 0
    to_drop: list[str] = []

    for uid, record in state.items():
        if not record.saved_path:
            # Message had no PDF; keep so we don't re-fetch
            continue
        p = Path(record.saved_path)
        if not p.exists():
            to_drop.append(uid)
            removed += 1

    for uid in to_drop:
        del state[uid]

    if removed:
        log.info("Reconciled state with disk: removed %d stale record(s)", removed)
        _save_state(paths, state)

    return removed


def _known_uids(state: dict[str, DownloadRecord]) -> set[str]:
    """
    UIDs we consider already done: present in state AND the file still exists on disk.
    If a file was manually deleted, the UID is excluded so it will be re-fetched.
    """
    result: set[str] = set()
    for uid, record in state.items():
        if record.saved_path and Path(record.saved_path).exists():
            result.add(uid)
        elif not record.saved_path:
            # Message had no PDF attachment — still skip it
            result.add(uid)
    return result


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def _unlock_pdf(data: bytes, password: str | None) -> tuple[bytes, bool]:
    """
    Try to open and re-save the PDF (removes encryption if password is correct).
    Returns (bytes_to_write, was_unlocked).
    Falls back to original bytes if pikepdf cannot open it at all.
    """
    def _try(pwd: str | None) -> bytes | None:
        try:
            pdf = pikepdf.open(io.BytesIO(data), password=pwd or "")
            buf = io.BytesIO()
            pdf.save(buf)
            pdf.close()
            return buf.getvalue()
        except Exception:
            return None

    if password:
        result = _try(password)
        if result is not None:
            return result, True

    result = _try(None)
    if result is not None:
        return result, True

    return data, False


def _pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name or not name.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            out.append((name, payload))
    return out


def _msg_month(msg: Message) -> str:
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m")
    except Exception:
        return "unknown"


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[-\s]+", "-", s).strip("-") or "unknown"


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\s]', "-", (s or "").strip()).strip("-")
    return s[:80] or "statement"


# ---------------------------------------------------------------------------
# IMAP connection and folder selection
# ---------------------------------------------------------------------------

def connect(loaded: LoadedConfig) -> imaplib.IMAP4:
    email, first_password = get_imap_credentials(loaded)
    passwords = get_imap_passwords_for_inbox(loaded, email)
    if not passwords:
        passwords = [first_password]
    host, port = loaded.config.imap.host, loaded.config.imap.port
    last_err: Exception | None = None
    for pwd in passwords:
        try:
            imap: imaplib.IMAP4 = (
                imaplib.IMAP4_SSL(host, port) if loaded.config.imap.use_ssl
                else imaplib.IMAP4(host, port)
            )
            imap.login(email, pwd)
            return imap
        except imaplib.IMAP4.error as e:
            last_err = e
            continue
    raise RuntimeError(
        "IMAP login failed. Check email and passwords in .local/config/secrets.json"
    ) from last_err


def _quote(folder: str) -> str:
    if folder.startswith('"'):
        return folder
    return f'"{folder}"' if any(c in folder for c in " []") else folder


def _select_folder(imap: imaplib.IMAP4, configured: str) -> str:
    """Select the best available folder; prefers All Mail so archived mail is found."""
    candidates: list[str] = []
    if configured.strip():
        candidates.append(configured.strip())

    try:
        _, listing = imap.list()
        for raw in (listing or []):
            if not raw:
                continue
            text = raw.decode("utf-8", errors="ignore")
            parts = text.rsplit(' "/" ', 1)
            name = parts[1].strip().strip('"') if len(parts) == 2 else ""
            if name and "all mail" in name.lower() and name not in candidates:
                candidates.append(name)
    except Exception:
        pass

    for fallback in ["[Gmail]/All Mail", "[Google Mail]/All Mail", "All Mail", "INBOX"]:
        if fallback not in candidates:
            candidates.append(fallback)

    for folder in candidates:
        try:
            typ, _ = imap.select(_quote(folder), readonly=True)
            if typ == "OK":
                return folder
        except imaplib.IMAP4.error:
            continue

    raise RuntimeError(f"Could not select any mailbox folder. Tried: {candidates}")


# ---------------------------------------------------------------------------
# Reunlock locked PDFs from previous runs (no IMAP needed)
# ---------------------------------------------------------------------------

def _reunlock_from_state(
    state: dict[str, DownloadRecord],
    loaded: LoadedConfig,
    paths: Paths,
) -> int:
    """
    For every record with unlocked=False and saved_path on disk, attempt unlock.
    Updates state in-place. Returns number of files successfully unlocked.
    """
    count = 0
    for uid, record in list(state.items()):
        if record.unlocked or not record.saved_path:
            continue
        p = Path(record.saved_path)
        if not p.exists():
            continue
        raw = p.read_bytes()
        password = resolve_password(loaded, record.bank, record.card)
        unlocked_data, did_unlock = _unlock_pdf(raw, password)
        if did_unlock and unlocked_data != raw:
            p.write_bytes(unlocked_data)
            state[uid] = DownloadRecord(
                uid=record.uid, bank=record.bank, card=record.card,
                filename=record.filename, saved_path=record.saved_path,
                fetched_at=record.fetched_at, unlocked=True,
            )
            count += 1
            log.info("Reunlocked: %s", p.name)
    if count:
        _save_state(paths, state)
    return count


# ---------------------------------------------------------------------------
# Per-rule fetch (parallel: 5 threads per bank/cc)
# ---------------------------------------------------------------------------

def _fetch_one_uid(
    uid: str,
    rule: BankEmailRule,
    paths: Paths,
    loaded: LoadedConfig,
    folder: str,
) -> tuple[str, list[str], DownloadRecord | None]:
    """
    Fetch a single message by UID using a dedicated IMAP connection.
    Returns (uid, saved_paths, record). Raises on connection/fetch errors.
    """
    imap = connect(loaded)
    try:
        _select_folder(imap, folder)
        typ, msg_data = imap.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"FETCH failed for uid {uid}")
        raw_msg = msg_data[0][1]
        if not isinstance(raw_msg, (bytes, bytearray)):
            raise RuntimeError(f"Invalid message for uid {uid}")
        msg = message_from_bytes(bytes(raw_msg))
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    month = _msg_month(msg)
    attachments = _pdf_attachments(msg)
    bank_slug = _slug(rule.bank)
    card_slug = _slug(rule.card or "default")

    if not attachments:
        return (
            uid,
            [],
            DownloadRecord(
                uid=uid, bank=rule.bank, card=rule.card or "",
                filename="", saved_path="",
                fetched_at=datetime.now(timezone.utc).isoformat(), unlocked=False,
            ),
        )

    saved_paths: list[str] = []
    first_record: DownloadRecord | None = None
    password = resolve_password(loaded, rule.bank, rule.card)

    for filename, payload in attachments:
        out_dir = paths.raw_pdfs_dir / bank_slug / card_slug
        out_dir.mkdir(parents=True, exist_ok=True)
        if rule.file_suffix and rule.file_suffix.strip():
            suffix = _safe_name(rule.file_suffix)
        else:
            stem = Path(filename).stem
            suffix = _safe_name(stem.split("_")[-1] if "_" in stem else stem)
        out_path = out_dir / f"{month}_{suffix}.pdf"
        n = 0
        while out_path.exists():
            n += 1
            out_path = out_dir / f"{month}_{suffix}_{n}.pdf"
        data_to_write, unlocked = _unlock_pdf(payload, password)
        out_path.write_bytes(data_to_write)
        saved_paths.append(str(out_path))
        rec = DownloadRecord(
            uid=uid, bank=rule.bank, card=rule.card or "",
            filename=filename, saved_path=str(out_path),
            fetched_at=datetime.now(timezone.utc).isoformat(), unlocked=unlocked,
        )
        if first_record is None:
            first_record = rec

    return (uid, saved_paths, first_record or None)


def _fetch_rule(
    imap: imaplib.IMAP4,
    rule: BankEmailRule,
    paths: Paths,
    loaded: LoadedConfig,
    state: dict[str, DownloadRecord],
    known: set[str],
    saved_paths: list[str],
    folder: str,
) -> RuleSummary:
    """
    Search, filter already-known UIDs, fetch and save new PDFs for one rule.
    Mutates state and known in-place.
    """
    tag = f"{rule.bank}/{rule.card or 'default'}"

    # Effective TO filter: rule.to_emails, else secrets inbox list, else first inbox email
    to_list: list[str] = []
    if rule.to_emails:
        to_list = [e.strip() for e in rule.to_emails if e and e.strip()]
    if not to_list:
        to_list = get_inbox_emails(loaded)

    # Build IMAP SEARCH query
    parts = [f'FROM "{rule.from_email}"']
    if to_list:
        to_criteria = " OR ".join(f'TO "{addr}"' for addr in to_list)
        if len(to_list) > 1:
            parts.append(f"({to_criteria})")
        else:
            parts.append(to_criteria)
    if rule.subject_contains:
        parts.append(f'SUBJECT "{rule.subject_contains}"')
    search_str = " ".join(parts)

    log.info("[%s] Searching: %s", tag, search_str)
    typ, data = imap.uid("SEARCH", None, search_str)
    if typ != "OK":
        log.warning("[%s] Search failed", tag)
        return RuleSummary(bank=rule.bank, card=rule.card or "default", found=0, skipped=0, downloaded=0)

    all_uid_bytes: list[bytes] = (data[0] or b"").split()
    # Newest first, capped
    all_uid_bytes = list(reversed(all_uid_bytes))[: loaded.config.imap.max_messages_per_rule]

    # Pre-filter: only UIDs we haven't fetched yet (or whose file was deleted)
    new_uid_bytes = [u for u in all_uid_bytes if u.decode("utf-8", errors="ignore").strip() not in known]
    skipped = len(all_uid_bytes) - len(new_uid_bytes)

    log.info("[%s] Found %d — %d already fetched, %d new", tag, len(all_uid_bytes), skipped, len(new_uid_bytes))

    downloaded = 0
    state_lock = Lock()

    def _collect(fut):
        nonlocal downloaded
        try:
            uid, paths_list, record = fut.result()
            with state_lock:
                if record:
                    state[uid] = record
                    known.add(uid)
                for p in paths_list:
                    saved_paths.append(p)
                    downloaded += 1
                _save_state(paths, state)
        except Exception as e:
            log.warning("[%s] uid %s: %s", tag, getattr(fut, "_uid", ""), e)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        log.info("[%s] Fetching %d message(s) with %d workers", tag, len(new_uid_bytes), FETCH_WORKERS)
        futures = []
        for uid_bytes in new_uid_bytes:
            uid = uid_bytes.decode("utf-8", errors="ignore").strip()
            f = executor.submit(_fetch_one_uid, uid, rule, paths, loaded, folder)
            f._uid = uid  # type: ignore[attr-defined]
            futures.append(f)
        for f in as_completed(futures):
            _collect(f)

    return RuleSummary(
        bank=rule.bank,
        card=rule.card or "default",
        found=len(all_uid_bytes),
        skipped=skipped,
        downloaded=downloaded,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_pdfs(paths: Paths | None = None) -> FetchResult:
    """
    For each email rule: search IMAP by FROM/SUBJECT, skip UIDs already in state
    (and whose file exists on disk), fetch and save new PDFs.
    Safe to rerun — already-fetched messages are skipped, deleted files are re-fetched.
    """
    paths = paths or get_paths()
    loaded = load_config(paths)

    if not loaded.config.email_rules:
        raise RuntimeError("No email rules. Add entries to .local/config/card_rules.json")

    state = _load_state(paths)
    _reconcile_state_with_disk(paths, state)
    known = _known_uids(state)
    log.info("State: %d record(s), %d UIDs to skip", len(state), len(known))

    # Attempt to unlock any PDFs that were saved locked in previous runs
    reunlocked = _reunlock_from_state(state, loaded, paths)
    if reunlocked:
        log.info("Reunlocked %d PDF(s) from previous runs", reunlocked)

    imap = connect(loaded)
    log.info("Connected to %s", loaded.config.imap.host)

    total_downloaded = 0
    total_skipped = 0
    saved_paths: list[str] = []
    rule_summaries: list[RuleSummary] = []
    folder = ""

    try:
        folder = _select_folder(imap, loaded.config.imap.folder)
        log.info("Folder: %s", folder)

        for rule in loaded.config.email_rules:
            summary = _fetch_rule(imap, rule, paths, loaded, state, known, saved_paths, folder)
            rule_summaries.append(summary)
            total_downloaded += summary.downloaded
            total_skipped += summary.skipped

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return FetchResult(
        downloaded=total_downloaded,
        skipped=total_skipped,
        reunlocked=reunlocked,
        folder=folder,
        saved_paths=saved_paths,
        rule_summaries=rule_summaries,
    )

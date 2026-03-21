from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator

from .paths import Paths, get_paths


class ImapConfig(BaseModel):
    host: str = "imap.gmail.com"
    port: int = 993
    use_ssl: bool = True
    folder: str = "[Gmail]/All Mail"
    max_messages_per_rule: int = 50


class BankEmailRule(BaseModel):
    bank: str
    card: str | None = None
    from_email: str
    to_emails: list[str] | None = None
    subject_contains: str | None = None
    file_suffix: str | None = None


class PasswordRule(BaseModel):
    bank: str
    card: str | None = None
    password_template: str


class CardRule(BaseModel):
    """Single entry per bank/card: from_emails and passwords lists."""
    bank: str
    card: str | None = None
    from_emails: list[str] = Field(default_factory=list, min_length=1)
    to_emails: list[str] | None = None
    passwords: list[str] = Field(default_factory=list, min_length=1)
    subject_contains: str | None = None
    file_suffix: str | None = None


class InboxCredential(BaseModel):
    """One inbox: email address and one or more passwords (e.g. for IMAP login)."""
    email: str
    passwords: list[SecretStr] = Field(default_factory=list, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_password_or_passwords(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        pwd = data.get("password")
        pwds = data.get("passwords")
        if pwd is not None and pwds is None:
            data = {**data, "passwords": [pwd]}
        elif isinstance(pwds, list) and len(pwds) == 0 and pwd is not None:
            data = {**data, "passwords": [pwd]}
        return data


class SecretsConfig(BaseModel):
    inboxes: list[InboxCredential] | None = None


class TagRule(BaseModel):
    """User-defined tag: matched against transaction descriptions via regex."""
    tag_name: str
    regex_patterns: list[str] = Field(default_factory=list, min_length=1)


class AppConfig(BaseModel):
    imap: ImapConfig = Field(default_factory=ImapConfig)
    email_rules: list[BankEmailRule] = Field(default_factory=list)
    password_rules: list[PasswordRule] = Field(default_factory=list)


@dataclass(frozen=True)
class CompiledTag:
    tag_name: str
    compiled: list[re.Pattern[str]]


@dataclass(frozen=True)
class LoadedConfig:
    paths: Paths
    config: AppConfig
    secrets: SecretsConfig
    tags: list[CompiledTag] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


EMAIL_RULES_FILE = "email_rules.json"
PASSWORD_RULES_FILE = "password_rules.json"
CARD_RULES_FILE = "card_rules.json"
TAGS_FILE = "tags.json"
APP_CONFIG_FILE = "app.json"
# Sample templates under docs/sample/ (committed repo examples)
DOCS_SAMPLE_DIR = "docs/sample"
SAMPLE_CARD_RULES_FILE = "card_rules.json"
SAMPLE_TAGS_FILE = "tags.json"
SAMPLE_SECRETS_FILE = "secrets.json"


def _expand_card_rules(card_rules_raw: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand card_rules into email_rules and password_rules (for AppConfig)."""
    email_rules_raw: list[dict[str, Any]] = []
    password_rules_raw: list[dict[str, Any]] = []
    for r in card_rules_raw:
        try:
            rule = CardRule.model_validate(r)
        except Exception:
            continue
        bank = rule.bank
        card = rule.card
        password_template = rule.passwords[0] if rule.passwords else ""
        password_rules_raw.append({
            "bank": bank,
            "card": card,
            "password_template": password_template,
        })
        for from_email in rule.from_emails:
            email_rules_raw.append({
                "bank": bank,
                "card": card,
                "from_email": from_email,
                "to_emails": rule.to_emails,
                "subject_contains": rule.subject_contains,
                "file_suffix": rule.file_suffix,
            })
    return email_rules_raw, password_rules_raw


def _load_tags(config_dir: Path) -> list[CompiledTag]:
    """Load and compile tag rules from tags.json (case-insensitive)."""
    tags_path = config_dir / TAGS_FILE
    if not tags_path.exists():
        return []
    raw = _read_json(tags_path)
    items = raw if isinstance(raw, list) else []
    compiled_tags: list[CompiledTag] = []
    for item in items:
        try:
            rule = TagRule.model_validate(item)
        except Exception:
            continue
        patterns = [re.compile(p, re.IGNORECASE) for p in rule.regex_patterns]
        compiled_tags.append(CompiledTag(tag_name=rule.tag_name, compiled=patterns))
    return compiled_tags


def compute_tags(description: str, tags: list[CompiledTag]) -> str:
    """Return space-separated tag names whose patterns match the description."""
    matched = []
    for tag in tags:
        if any(p.search(description) for p in tag.compiled):
            matched.append(tag.tag_name)
    return " ".join(matched)


def load_config(paths: Paths | None = None) -> LoadedConfig:
    paths = paths or get_paths()
    config_dir = paths.local_config_dir
    secrets_path = config_dir / "secrets.json"
    app_path = config_dir / APP_CONFIG_FILE

    email_rules_raw: list[dict[str, Any]] = []
    password_rules_raw: list[dict[str, Any]] = []

    card_rules_path = config_dir / CARD_RULES_FILE
    if card_rules_path.exists():
        raw = _read_json(card_rules_path)
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("cards") or raw.get("card_rules") or []
        else:
            items = []
        if isinstance(items, list):
            email_rules_raw, password_rules_raw = _expand_card_rules(items)
    if not email_rules_raw:
        email_rules_path = config_dir / EMAIL_RULES_FILE
        if email_rules_path.exists():
            raw = _read_json(email_rules_path)
            email_rules_raw = raw if isinstance(raw, list) else (raw.get("email_rules", []) if isinstance(raw, dict) else [])
        elif app_path.exists():
            app_raw = _read_json(app_path)
            email_rules_raw = app_raw.get("email_rules", []) if isinstance(app_raw, dict) else []
    if not password_rules_raw:
        password_rules_path = config_dir / PASSWORD_RULES_FILE
        if password_rules_path.exists():
            raw = _read_json(password_rules_path)
            password_rules_raw = raw if isinstance(raw, list) else (raw.get("password_rules", []) if isinstance(raw, dict) else [])
        elif app_path.exists():
            app_raw = _read_json(app_path)
            password_rules_raw = app_raw.get("password_rules", []) if isinstance(app_raw, dict) else []

    imap_overrides: dict[str, Any] = {}
    if app_path.exists():
        app_raw = _read_json(app_path)
        if isinstance(app_raw, dict) and "imap" in app_raw and isinstance(app_raw["imap"], dict):
            imap_overrides = app_raw["imap"]

    config = AppConfig(
        imap=ImapConfig.model_validate(imap_overrides) if imap_overrides else ImapConfig(),
        email_rules=[BankEmailRule.model_validate(r) for r in email_rules_raw],
        password_rules=[PasswordRule.model_validate(r) for r in password_rules_raw],
    )
    secrets = SecretsConfig.model_validate(_read_json(secrets_path))
    tags = _load_tags(config_dir)

    return LoadedConfig(paths=paths, config=config, secrets=secrets, tags=tags)


def get_imap_credentials(loaded: LoadedConfig) -> tuple[str, str]:
    """
    Return (email, password) for IMAP login. Uses first inbox and first password;
    if that fails, caller can try next password. Raises RuntimeError if no credentials.
    """
    if not loaded.secrets.inboxes or len(loaded.secrets.inboxes) == 0:
        raise RuntimeError(
            "Set inboxes (email + passwords) in .local/config/secrets.json"
        )
    inbox = loaded.secrets.inboxes[0]
    if not inbox.passwords:
        raise RuntimeError(
            "First inbox in secrets.json must have at least one password"
        )
    return inbox.email, inbox.passwords[0].get_secret_value()


def get_imap_passwords_for_inbox(loaded: LoadedConfig, email: str) -> list[str]:
    """Return list of passwords to try for the given inbox email (for login retries)."""
    if not loaded.secrets.inboxes:
        return []
    for inbox in loaded.secrets.inboxes:
        if (inbox.email or "").strip().lower() == (email or "").strip().lower():
            return [p.get_secret_value() for p in inbox.passwords]
    return []


def get_inbox_emails(loaded: LoadedConfig) -> list[str]:
    """List of inbox emails (for TO filter default). From secrets.inboxes."""
    if not loaded.secrets.inboxes:
        return []
    return [i.email.strip() for i in loaded.secrets.inboxes if i.email and i.email.strip()]


def resolve_password(loaded: LoadedConfig, bank: str, card: str | None) -> str | None:
    """
    Return PDF password for (bank, card) from the matching password_rule.
    Returns None if no matching rule.
    """
    bank_n = (bank or "").strip().lower()
    card_n = (card or "").strip().lower()
    for rule in loaded.config.password_rules:
        if (rule.bank or "").strip().lower() != bank_n:
            continue
        if (rule.card or "").strip().lower() != card_n:
            continue
        return rule.password_template
    return None


def ensure_local_dirs(paths: Paths | None = None) -> Paths:
    paths = paths or get_paths()
    paths.local_config_dir.mkdir(parents=True, exist_ok=True)
    paths.local_state_dir.mkdir(parents=True, exist_ok=True)

    paths.raw_pdfs_dir.mkdir(parents=True, exist_ok=True)
    paths.normalized_dir.mkdir(parents=True, exist_ok=True)
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    return paths


def write_config_templates(paths: Paths | None = None) -> None:
    paths = ensure_local_dirs(paths)
    config_dir = paths.local_config_dir
    secrets_path = config_dir / "secrets.json"
    card_rules_path = config_dir / CARD_RULES_FILE
    tags_path = config_dir / TAGS_FILE

    if not card_rules_path.exists():
        sample_cr = paths.repo_root / DOCS_SAMPLE_DIR / SAMPLE_CARD_RULES_FILE
        if sample_cr.is_file():
            try:
                raw_list = json.loads(sample_cr.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw_list = []
            merged: list[dict[str, Any]] = []
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    entry: dict[str, Any] = dict(item)
                    if "passwords" not in entry:
                        entry["passwords"] = ["your-pdf-password"]
                    if "card" not in entry:
                        entry["card"] = f"card-{len(merged) + 1}"
                    merged.append(entry)
            if merged:
                card_rules_path.write_text(
                    json.dumps(merged, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(
                    "\nCreated .local/config/card_rules.json from docs/sample/card_rules.json.\n"
                    "Set each card name and PDF passwords; add subject_contains or to_emails if needed.\n",
                    file=sys.stderr,
                )
        if not card_rules_path.exists():
            card_rules_path.write_text(
                json.dumps(
                    [
                        {
                            "bank": "example-bank",
                            "card": "example-card",
                            "from_emails": ["statements@example.com"],
                            "to_emails": ["your.email@gmail.com"],
                            "passwords": ["your-pdf-password"],
                            "subject_contains": "Statement",
                        }
                    ],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    if not secrets_path.exists():
        sample_sec = paths.repo_root / DOCS_SAMPLE_DIR / SAMPLE_SECRETS_FILE
        if sample_sec.is_file():
            secrets_path.write_text(sample_sec.read_text(encoding="utf-8"), encoding="utf-8")
            print(
                "\nCreated .local/config/secrets.json from docs/sample/secrets.json.\n"
                "Replace with your real inbox email and IMAP app password.\n",
                file=sys.stderr,
            )
        else:
            secrets_path.write_text(
                json.dumps(
                    {
                        "inboxes": [
                            {
                                "email": "your.email@gmail.com",
                                "passwords": ["xxxx xxxx xxxx xxxx"],
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    if not tags_path.exists():
        sample_path = paths.repo_root / DOCS_SAMPLE_DIR / SAMPLE_TAGS_FILE
        if sample_path.is_file():
            tags_path.write_text(sample_path.read_text(encoding="utf-8"), encoding="utf-8")
            print(
                "\nCreated .local/config/tags.json from docs/sample/tags.json.\n"
                "Edit it with your own tag_name and regex_patterns, then re-run export if needed.\n",
                file=sys.stderr,
            )
        else:
            tags_path.write_text(
                json.dumps(
                    [
                        {"tag_name": "UBER", "regex_patterns": ["uber"]},
                        {"tag_name": "ZOMATO", "regex_patterns": ["zomato"]},
                        {"tag_name": "AMAZON", "regex_patterns": ["amazon"]},
                        {"tag_name": "SWIGGY", "regex_patterns": ["swiggy"]},
                    ],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

# Card rules (IMAP + passwords)

ccsa uses **`.local/config/card_rules.json`** (gitignored): one entry per bank/card with **`from_emails`** and **`passwords`** lists. Optional **`app.json`** can contain an **`imap`** block to override server/folder.

---

## card_rules.json

Array of card rules. Each rule is expanded into one IMAP search per `from_email` (same bank/card); the first entry in **`passwords`** is used to decrypt PDFs for that card.

**Sample (repo):** **[sample/card_rules.json](sample/card_rules.json)** lists only **`bank`** and **`from_emails`** (public statement-sender addresses you can look up per bank). It does **not** include real card names or passwords. On first setup, **`ccsa init`** merges that sample into **`.local/config/card_rules.json`**, adding placeholder **`card`** (`card-1`, `card-2`, …) and **`passwords`**: `["your-pdf-password"]` so the file validates — replace those with your card labels and PDF passwords. If **`docs/sample/card_rules.json`** is missing, a generic one-card template is written instead.

| Field | Required | Description |
|-------|----------|-------------|
| `bank` | Yes | Bank name (e.g. `HDFC`, `SBI`). Used in folder `data/raw-pdfs/<bank>/<card>/`. |
| `card` | No | Card name (e.g. `Diners Privilege`, `NEO`). Omit or use `null` for a single card. |
| `from_emails` | Yes | List of sender addresses to search (IMAP FROM). One search per address; all save to same bank/card folder. |
| `to_emails` | No | List of recipient (inbox) addresses — only fetch emails sent TO one of these. If omitted, uses the list of **`email`** values from **`secrets.json`** → **`inboxes`**. |
| `passwords` | Yes | List of PDF password templates. First one is used for decrypting statement PDFs. |
| `subject_contains` | No | Substring that must appear in the subject (applies to all from_emails for this card). |
| `file_suffix` | No | Custom suffix for saved PDFs: `YYYY-MM_{file_suffix}.pdf`. |

### Example

There is **no** committed JSON here with real credentials. Use **[sample/card_rules.json](sample/card_rules.json)** for public sender addresses; run **`ccsa init`** to create **`.local/config/card_rules.json`** and fill in **`card`**, **`passwords`**, and optional **`subject_contains`** from your bank's emails.

Minimal shape:

```json
[
  {
    "bank": "MyBank",
    "card": "Rewards",
    "from_emails": ["statements@mybank.example"],
    "passwords": ["your-pdf-password"]
  }
]
```



Placeholders in **`passwords`** are not supported; use the actual PDF password string in **`card_rules.json`**. Do not commit `card_rules.json` or `secrets.json`.

---

## Fallback: email_rules.json + password_rules.json

If **`card_rules.json`** is missing, ccsa loads **`email_rules.json`** (array of `bank`, `card`, `from_email`, `subject_contains?`, …) and **`password_rules.json`** (array of `bank`, `card`, `password_template`). You can migrate to **`card_rules.json`** and remove the old files.

---

## secrets.json

A committed example lives at **[sample/secrets.json](sample/secrets.json)** (`you@example.com` + placeholder app password). On first setup, **`ccsa init`** copies it to **`.local/config/secrets.json`** if missing; replace with your real inbox and [app password](https://support.google.com/accounts/answer/185833). If **`docs/sample/secrets.json`** is absent, a generic inline template is written instead.

- **`inboxes`** — list of inbox credentials for IMAP. Each entry has:
  - **`email`** — the inbox address (e.g. Gmail address).
  - **`passwords`** — list of passwords to try for login (e.g. one or more app passwords). You can use **`password`** (singular) as a shorthand for one password.
  - For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833) (see [IMAP_SETUP.md](IMAP_SETUP.md)); in config you just set `email` and `passwords`.

The default **TO** filter (when a card rule omits `to_emails`) is the list of all `email` values from `inboxes`. Do not commit `secrets.json`.

---

## After editing config

1. Set **`inboxes`** (email + passwords) in **`secrets.json`**. For Gmail, use an app password — see [IMAP_SETUP.md](IMAP_SETUP.md).
2. Run **`ccsa imap fetch`** to sync. Safe to rerun; state is in `.local/state/imap_fetched.json` and reconciled with the data directory.

If the IMAP folder is unavailable, ccsa falls back through `[Gmail]/All Mail`, `All Mail`, `INBOX`.

---

## tags.json (transaction labels)

Optional **`.local/config/tags.json`**: a JSON **array** of rules. Each rule has:

| Field | Description |
|-------|-------------|
| `tag_name` | Label written to the **`tags`** column in `master.csv` (space-separated if several match). |
| `regex_patterns` | List of patterns; **any** match tags the row. Matching is **case-insensitive**. |

On first setup, **`ccsa init`** (or any command that creates config templates) copies **[sample/tags.json](sample/tags.json)** to **`.local/config/tags.json`** if that file is missing; edit it with your own patterns. If **`docs/sample/tags.json`** is not present (e.g. custom install), a small built-in default list is written instead. Use standard Python `re` syntax (e.g. `\\.` for a literal dot, `\\b` for word boundaries).

After changing tags, regenerate the export: **`ccsa export master`** (no PDF re-parse needed).

# CardQL user guide

## Install

1. Install **Python 3.9+** and create a virtual environment.
2. From the project folder: `pip install -r requirements.txt` then `pip install .`
3. Install **Ollama** from [ollama.com](https://ollama.com) if you want chat features.

## First-time setup

1. Run **`cardql init`** (or just **`cardql`**, which also ensures folders exist).
2. Edit **`.local/config/secrets.json`** with your email IMAP credentials (see [IMAP_SETUP.md](IMAP_SETUP.md)).
3. Edit **`.local/config/card_rules.json`** with your banks/cards, sender addresses, and PDF passwords (see [CONFIG.md](CONFIG.md)).

## Daily use

- **`cardql`** — Full automation: fetch new PDFs, update your spreadsheet database, start Ollama if needed, open the Streamlit chat app.
- **`cardql parse`** — Refresh **master.csv** and **transactions.sqlite** from your PDFs (and open the CSV in Excel/Numbers/etc. unless you pass **`--no-open`**).
- **`cardql ui`** — Open only the chat interface.
- **`cardql sql`** — Open a terminal **SQLite** session to run your own SQL.

For questions in plain English, use the **Streamlit** app (**`cardql ui`**). Technical details: [LLM_QUERY.md](LLM_QUERY.md).

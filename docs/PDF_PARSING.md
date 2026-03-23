# Parsing transaction data from statement PDFs

This project parses credit card statement PDFs into a **normalized JSON format** (see `parsers/schema.py` and [ARCHITECTURE.md](ARCHITECTURE.md)). Parsers are named by bank and variant (e.g. `hdfc_v1`, `hdfc_v2`). For each PDF, **all** parser variants for that bank are run; the **Statement with the most transactions** is used (hit-and-try). A successful parse with **0 transactions** is valid (e.g. card not used that month). A **warning** is logged only when **every** variant raises (no parser could parse the format); then the PDF is skipped.

## Flow

1. **Extract text** from the PDF with **pypdf** (`cardql.ingest.pdf.extract_text_from_pdf`).  
   If the PDF is password-protected, decrypt it first with **pikepdf** (same as IMAP fetch does).

2. **Run bank parser(s)** on the extracted text.  
   The registry maps bank slug → list of (parser_name, parser_func). **Every** variant is run (exceptions are skipped). The result with the **largest number of transactions** is chosen. A parse with **0 transactions** is still a valid result (e.g. card not used). Only when **no** variant succeeds (all raise) is a warning logged and `try_parse_with_bank` returns `None`.

3. **Output** is a `Statement` (period, source path, list of `Transaction`).

## Parsers (bank_v1, bank_v2, …)

| Bank     | Module       | Format / card |
|----------|--------------|----------------|
| Axis     | `axis_v1`    | Neo-style: DD/MM/YYYY description category amount Dr\|Cr |
| HDFC     | `hdfc_v1`    | Diners (old): DD/MM/YYYY [time] description amount [Cr] |
| HDFC     | `hdfc_v2`    | Diners (new): DATE \| TIME DESCRIPTION C amount l, credits: + C amount |
| HSBC     | `hsbc_v1`    | TravelOne-style: DDMMM description amount (concatenated text) |
| ICICI    | `icici_v1`   | Sapphiro-style: DD/MM/YYYY serial description amount [CR] |
| IndusInd | `indusind_v1`| EazyDiner: DD/MM/YYYY description category amount DR\|CR |
| SBI      | `sbi_v1`     | Elite/Cashback: DD MMM YY description amount D\|C\|M (card from path) |

## CLI

From the repo root (with `PYTHONPATH=src` or installed package):

```bash
# Normalize all PDFs, merge to master.csv + SQLite, open CSV
cardql parse

# One PDF → JSON file only
cardql parse data/raw-pdfs/axis/neo/2025-03_statement.pdf -o /tmp/out.json
```

Bank/card are inferred from the path (`.../raw-pdfs/<bank>/<card>/...`).

## Code layout

- **`src/cardql/ingest/pdf.py`** – `extract_text_from_pdf(data: bytes) -> str` (pypdf). Decryption via `cardql.ingest.imap.unlock_pdf`.

- **`src/cardql/parsers/schema.py`** – `Transaction` and `Statement` (Pydantic models).

- **`src/cardql/parsers/banks/`** – One module per bank variant (e.g. `axis_v1.py`, `hdfc_v1.py`). Each exposes `parse_<bank>_v1(text, source_pdf_path=..., bank=..., card=...) -> Statement`.

- **`src/cardql/parsers/registry.py`** – `_BANK_PARSERS` (bank_slug → list of (name, parser_func)), `get_parsers_for_bank`, `try_parse_with_bank` (tries all, picks result with most transactions; 0 txns is valid; logs warning only when all variants raise), `get_parser`, `list_parsers`.

- **`src/cardql/cli/`** – `cardql parse` (normalize + export + open CSV), `cardql fetch`, etc.

## Adding another bank or variant

1. Add `src/cardql/parsers/banks/<bank>_v1.py` (or `<bank>_v2.py` for a second variant) with a function `parse_<bank>_vN(text, source_pdf_path=None, bank=..., card=...) -> Statement`.
2. Register it in `registry.py`: append to `_BANK_PARSERS["<bank>"]` (or add the key if new bank). Import the new parser from `.banks.<bank>_v1` in `registry.py` and add it to the list.

## Dependencies

- **pikepdf** – decrypt/unlock PDFs (IMAP fetch).
- **pypdf** – extract text from PDFs (`requirements.txt`).

For table-heavy or complex layouts, consider **pdfplumber** for more robust table extraction.

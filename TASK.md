# Task: Credit card statement parser

**Status:** IMAP fetch implemented (card_rules.json, 5 workers, UID state, disk reconciliation). Normalize/export/analyze planned.

---

I receive credit card statements from multi banks/cards in my mail box. They are password protected but have simple pattern DDMMYYYY type of birthdate etc. I want to create a system to parse each statement and compile into an excel or some other format for better analysis. How to do this?

0. We need a mapping of bank to which email id they send the statement from. We also need a mapping of passwords of each PDF. Both of these should be stored securely in config json files not to be committed, and can be initialised through a cli tool
1. Use **IMAP** to search for each email pattern and fetch new statements (implemented: `ccsa imap fetch`, config in `card_rules.json`, 5 workers per rule)
2. Download the PDFs neatly into a folder under **`data/raw-pdfs/`** (gitignored) — done as part of `ccsa imap fetch`
3. Create a common format in which we can convert each PDF into a JSON
4. Finally give an interface where we can ask common queries. Would be helpful to have a master table with columns like date,bank,card,merchant,amount etc. and also an overall summary of spends on each card per month
5. Keep a modular architecture, plan to keep code structure in an extensible way

Create PLAN.md with the plan for this project
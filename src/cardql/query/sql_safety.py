from __future__ import annotations

import re

_FORBIDDEN = (
    " ATTACH ", " PRAGMA ", " INSERT ", " UPDATE ", " DELETE ",
    " DROP ", " CREATE ", " ALTER ", " REPLACE ", " VACUUM ", " DETACH ",
)


def validate_select_sql(raw: str) -> tuple[bool, str]:
    """Allow a single SQLite ``SELECT`` only.  Returns ``(ok, sql_or_error)``."""
    s = (raw or "").strip()
    if not s:
        return False, "Empty SQL"
    if s.endswith(";"):
        s = s[:-1].strip()
    if ";" in s:
        return False, "Only one SQL statement allowed (no ; in the middle)"
    if not re.match(r"(?is)\s*select\b", s):
        return False, "Only SELECT queries are allowed"
    padded = f" {s.upper()} "
    for bad in _FORBIDDEN:
        if bad in padded:
            return False, f"Forbidden keyword in query: {bad.strip()}"
    return True, s


def fix_or_precedence(sql: str) -> str:
    """Wrap ``… LIKE … OR … LIKE …`` in parentheses when followed by ``AND``."""
    upper = sql.upper()
    where_pos = upper.find(" WHERE ")
    if where_pos < 0:
        return sql

    after_where = where_pos + 7
    where_end = len(sql)
    for kw in (" GROUP ", " ORDER ", " LIMIT ", " HAVING "):
        idx = upper.find(kw, after_where)
        if 0 <= idx < where_end:
            where_end = idx

    where_clause = sql[after_where:where_end]
    wc_upper = where_clause.upper()

    if " OR " not in wc_upper or " AND " not in wc_upper:
        return sql

    or_idx = wc_upper.find(" OR ")

    depth = 0
    for i in range(or_idx):
        if where_clause[i] == "(":
            depth += 1
        elif where_clause[i] == ")":
            depth -= 1
    if depth > 0:
        return sql

    search_from = or_idx + 4
    and_idx = -1
    d = 0
    while search_from < len(wc_upper) - 4:
        ch = where_clause[search_from]
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif d == 0 and wc_upper[search_from : search_from + 5] == " AND ":
            and_idx = search_from
            break
        search_from += 1

    if and_idx < 0:
        return sql

    fixed = "(" + where_clause[:and_idx] + ")" + where_clause[and_idx:]
    return sql[:after_where] + fixed + sql[where_end:]

from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```\w*\s*([\s\S]*?)```", re.IGNORECASE)
_THINK_WRAPPER = re.compile(
    r"(?:```\s*think\s*[\s\S]*?```|`think[\s\S]*?`)",
    re.IGNORECASE | re.DOTALL,
)


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first ``{…}`` JSON object from *text* (tolerates wrappers)."""
    t = (text or "").strip()
    t = _THINK_WRAPPER.sub("", t).strip()
    m = _JSON_FENCE.search(t)
    if m:
        t = m.group(1).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return _loads_json_lenient(t[start : i + 1])
    raise ValueError("Unbalanced JSON in model output")


def _loads_json_lenient(raw: str) -> dict[str, Any]:
    """``json.loads`` with repairs for trailing ``;`` / ``,`` and duplicate ``sql`` keys."""

    def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k == "sql" and v in (None, "") and "sql" in out:
                continue
            out[k] = v
        return out

    for candidate in (raw, re.sub(r";\s*}", "}", raw), re.sub(r",\s*}", "}", raw)):
        try:
            return json.loads(candidate, object_pairs_hook=_object_pairs)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("Could not parse JSON even after repairs", raw, 0)


def extract_sql_from_llm_response(text: str) -> str | None:
    """Robustly pull a ``SELECT`` out of whatever the model returned."""
    t = (text or "").strip()
    if not t:
        return None
    t = _THINK_WRAPPER.sub("", t).strip()
    if not t:
        return None

    try:
        data = extract_json_object(t)
        for key in ("sql", "query", "SQL"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().rstrip(";").strip() or None
    except (ValueError, json.JSONDecodeError):
        pass

    m = _CODE_FENCE.search(t)
    if m:
        inner = m.group(1).strip().rstrip(";").strip()
        if re.match(r"(?i)\s*SELECT\b", inner):
            return inner

    match = re.search(r"(SELECT\b[\s\S]+?)(?:;|\Z)", t, re.IGNORECASE)
    if match:
        sql = match.group(1).strip()
        return sql or None

    return None

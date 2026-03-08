"""
Bank statement parsers (axis_v1, hdfc_v1, ...). All parser variants live here.
Each module exports a single `parse` function; we re-export as parse_<bank>_vN.
"""

from __future__ import annotations

from .axis_v1 import parse as parse_axis_v1
from .hdfc_v1 import parse as parse_hdfc_v1
from .hdfc_v2 import parse as parse_hdfc_v2
from .hsbc_v1 import parse as parse_hsbc_v1
from .icici_v1 import parse as parse_icici_v1
from .indusind_v1 import parse as parse_indusind_v1
from .sbi_v1 import parse as parse_sbi_v1

__all__ = [
    "parse_axis_v1",
    "parse_hdfc_v1",
    "parse_hdfc_v2",
    "parse_hsbc_v1",
    "parse_icici_v1",
    "parse_indusind_v1",
    "parse_sbi_v1",
]

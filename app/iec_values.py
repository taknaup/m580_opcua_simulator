"""IEC 61131-3 elementary types as used by Control Expert, and literal parsing."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TypeInfo:
    name: str          # canonical Control Expert type name
    ua_type: str       # asyncua VariantType member name
    kind: str          # bool | int | float | str
    lo: float | None = None
    hi: float | None = None
    max_len: int | None = None


_INT_RANGES = {
    "SINT": ("SByte", -2**7, 2**7 - 1),
    "INT": ("Int16", -2**15, 2**15 - 1),
    "DINT": ("Int32", -2**31, 2**31 - 1),
    "LINT": ("Int64", -2**63, 2**63 - 1),
    "USINT": ("Byte", 0, 2**8 - 1),
    "BYTE": ("Byte", 0, 2**8 - 1),
    "UINT": ("UInt16", 0, 2**16 - 1),
    "WORD": ("UInt16", 0, 2**16 - 1),
    "UDINT": ("UInt32", 0, 2**32 - 1),
    "DWORD": ("UInt32", 0, 2**32 - 1),
    "ULINT": ("UInt64", 0, 2**64 - 1),
    "LWORD": ("UInt64", 0, 2**64 - 1),
    # TIME is a 32-bit millisecond count on M580
    "TIME": ("UInt32", 0, 2**32 - 1),
}

_STRING_RE = re.compile(r"^STRING(?:\[(\d+)\])?$", re.I)
DEFAULT_STRING_LEN = 16


def type_info(type_name: str) -> TypeInfo | None:
    """Return type info for an elementary type, or None if not elementary."""
    t = type_name.strip().upper()
    if t in ("BOOL", "EBOOL"):
        return TypeInfo(t, "Boolean", "bool")
    if t in _INT_RANGES:
        ua, lo, hi = _INT_RANGES[t]
        return TypeInfo(t, ua, "int", lo, hi)
    if t == "REAL":
        return TypeInfo(t, "Float", "float")
    if t == "LREAL":
        return TypeInfo(t, "Double", "float")
    if t in ("DATE", "TOD", "TIME_OF_DAY", "DT", "DATE_AND_TIME"):
        return TypeInfo(t, "String", "str", max_len=32)
    m = _STRING_RE.match(t)
    if m:
        n = int(m[1]) if m[1] else DEFAULT_STRING_LEN
        return TypeInfo(f"STRING[{n}]", "String", "str", max_len=n)
    return None


def default_value(ti: TypeInfo):
    return {"bool": False, "int": 0, "float": 0.0, "str": ""}[ti.kind]


_TIME_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)", re.I)
_TIME_MS = {"d": 86_400_000, "h": 3_600_000, "m": 60_000, "s": 1000, "ms": 1}


def _parse_int(text: str) -> int:
    t = text.replace("_", "")
    if "#" in t:
        # optional type prefix (INT#5, DINT#16#FF) then optional base
        parts = t.split("#")
        if parts[0].isalpha():
            parts = parts[1:]
        if len(parts) == 2:
            return int(parts[1], int(parts[0]))
        t = parts[0]
    if "." in t or "e" in t.lower():
        f = float(t)
        if not f.is_integer():
            raise ValueError("expected an integer")
        return int(f)
    return int(t)


def _parse_time(text: str) -> int:
    t = text.replace("_", "")
    if "#" in t:
        t = t.split("#", 1)[1]
    if re.fullmatch(r"\d+", t):
        return int(t)
    total, pos = 0.0, 0
    for m in _TIME_PART.finditer(t):
        if m.start() != pos:
            raise ValueError(f"bad TIME literal {text!r}")
        total += float(m[1]) * _TIME_MS[m[2].lower()]
        pos = m.end()
    if pos != len(t) or pos == 0:
        raise ValueError(f"bad TIME literal {text!r}")
    return int(total)


def _parse_string(text: str) -> str:
    t = text
    if len(t) >= 2 and t[0] == t[-1] == "'":
        t = t[1:-1]
    # IEC $-escapes
    out, i = [], 0
    esc = {"$": "$", "'": "'", "L": "\n", "N": "\n", "P": "\f", "R": "\r", "T": "\t"}
    while i < len(t):
        if t[i] == "$" and i + 1 < len(t):
            nxt = t[i + 1]
            if nxt.upper() in esc:
                out.append(esc[nxt.upper()]); i += 2; continue
            if i + 2 < len(t) and re.fullmatch(r"[0-9A-Fa-f]{2}", t[i + 1:i + 3]):
                out.append(chr(int(t[i + 1:i + 3], 16))); i += 3; continue
        out.append(t[i]); i += 1
    return "".join(out)


def parse_literal(text: str, ti: TypeInfo):
    """Parse an initial-value literal from an XEF file into a Python value."""
    t = text.strip()
    if ti.kind == "bool":
        u = t.upper().split("#")[-1]
        if u in ("TRUE", "1"):
            return True
        if u in ("FALSE", "0"):
            return False
        raise ValueError(f"bad BOOL literal {text!r}")
    if ti.kind == "int":
        return coerce(_parse_time(t) if ti.name == "TIME" else _parse_int(t), ti)
    if ti.kind == "float":
        t = t.split("#")[-1].replace("_", "")
        return coerce(float(t), ti)
    return coerce(_parse_string(t), ti)


def coerce(value, ti: TypeInfo):
    """Validate/convert a value (from the UI or a literal) to the tag's type."""
    if ti.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().upper() in ("TRUE", "FALSE", "1", "0"):
            return value.strip().upper() in ("TRUE", "1")
        raise ValueError("expected TRUE/FALSE")
    if ti.kind == "int":
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, str):
            try:
                value = _parse_time(value) if ti.name == "TIME" else _parse_int(value.strip())
            except ValueError:
                raise ValueError("expected a duration like t#1m30s or milliseconds"
                                 if ti.name == "TIME" else "expected an integer") from None
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("expected an integer")
            value = int(value)
        if not isinstance(value, int):
            raise ValueError("expected an integer")
        if not (ti.lo <= value <= ti.hi):
            raise ValueError(f"{ti.name} range is {ti.lo}..{ti.hi}")
        return value
    if ti.kind == "float":
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                raise ValueError("expected a number") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        if ti.name == "REAL" and abs(value) > 3.4028234663852886e38:
            raise ValueError("outside REAL range")
        return float(value)
    if not isinstance(value, str):
        value = str(value)
    if ti.max_len is not None and len(value) > ti.max_len:
        raise ValueError(f"{ti.name} holds at most {ti.max_len} characters")
    return value

"""Extract HMI-exposed variables from a Control Expert .XEF / .ZEF export.

Only variables in <dataBlock> with attribute IsVariableHMI = -1 are used. Structured
types (DDTs, DFB public interfaces, arrays) are expanded down to elementary leaf tags.

Comment and initial value of a leaf are resolved through a "chain" of XML elements,
lowest priority first: the member definition in its DDT, then any instanceElementDesc
overrides from enclosing DDT definitions, and finally the overrides on the instance.

Access is taken from the resolved comment prefix: "R/O" = read only, "R/W" = read/write.
"""
from __future__ import annotations

import io
import itertools
import re
import zipfile
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET

from .iec_values import TypeInfo, default_value, parse_literal, type_info

MAX_TAGS = 250_000
MAX_XEF_BYTES = 512 * 1024 * 1024

_ARRAY_RE = re.compile(r"^ARRAY\s*\[(.+?)\]\s*OF\s+(.+)$", re.I | re.S)
_ACCESS_RE = re.compile(r"^\s*R\s*/\s*([OW])\b", re.I)


@dataclass
class Tag:
    path: str
    type_name: str
    ti: TypeInfo
    initial: object
    writable: bool
    access: str            # "R/O", "R/W", or "default" (no prefix in the comment)
    comment: str
    context: list[str] = field(default_factory=list)  # comments of enclosing structures
    address: str = ""      # topological address (%MW..), if located


@dataclass
class Program:
    name: str
    version: str
    product: str
    plc: str
    source_file: str
    tags: list[Tag]
    warnings: list[str]
    hmi_variables: int
    struct_comments: dict[str, str] = field(default_factory=dict)  # structure path -> comment
    struct_types: dict[str, str] = field(default_factory=dict)     # structure path -> DDT/array type


class ParseError(Exception):
    pass


def _load_xef_bytes(data: bytes, filename: str) -> bytes:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [i for i in zf.infolist() if i.filename.lower().endswith(".xef")]
        if not members:
            raise ParseError(f"{filename}: archive contains no .xef file")
        members.sort(key=lambda i: i.filename.lower() != "unitpro.xef")
        info = members[0]
        if info.file_size > MAX_XEF_BYTES:
            raise ParseError(f"{filename}: {info.filename} is too large")
        return zf.read(info)


def _attr(el: ET.Element, name: str) -> str | None:
    for a in el.findall("attribute"):
        if a.get("name") == name:
            return a.get("value")
    return None


def _children_by_name(el: ET.Element) -> dict[str, ET.Element]:
    return {c.get("name"): c for c in el.findall("instanceElementDesc")}


class _Expander:
    def __init__(self, root: ET.Element, default_writable: bool):
        self.default_writable = default_writable
        self.ddts = {d.get("DDTName"): d for d in root.iter("DDTSource")}
        self.fbs = {f.get("nameOfFBType"): f for f in root.iter("FBSource")}
        self.efbs = {f.get("nameOfEFBType") for f in root.iter("EFBSource")}
        self.tags: list[Tag] = []
        self.struct_comments: dict[str, str] = {}
        self.struct_types: dict[str, str] = {}
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self._override_cache: dict[int, dict[str, ET.Element]] = {}

    def warn_once(self, key: str, msg: str):
        if key not in self._warned:
            self._warned.add(key)
            self.warnings.append(msg)

    def _overrides(self, el: ET.Element) -> dict[str, ET.Element]:
        k = id(el)
        if k not in self._override_cache:
            self._override_cache[k] = _children_by_name(el)
        return self._override_cache[k]

    def _child_chain(self, chain: list[ET.Element], name: str) -> list[ET.Element]:
        return [o for el in chain if (o := self._overrides(el).get(name)) is not None]

    @staticmethod
    def _resolve_comment(chain: list[ET.Element], inherited: str) -> str:
        comment = inherited
        for el in chain:
            text = el.findtext("comment")
            if text and text.strip():
                comment = text.strip()
        return comment

    @staticmethod
    def _resolve_init(chain: list[ET.Element]) -> str | None:
        value = None
        for el in chain:
            vi = el.find("variableInit")
            if vi is not None and vi.get("value") is not None:
                value = vi.get("value")
            v = el.find("value")
            if v is not None and v.text is not None:
                value = v.text
        return value

    def _members(self, type_name: str) -> list[ET.Element] | None:
        if type_name in self.ddts:
            st = self.ddts[type_name].find("structure")
            return st.findall("variables") if st is not None else []
        if type_name in self.fbs:
            fb = self.fbs[type_name]
            out = []
            for sect in ("inputParameters", "outputParameters", "publicLocalVariables"):
                s = fb.find(sect)
                if s is not None:
                    out += s.findall("variables")
            return out
        return None

    def expand(self, path: str, type_name: str, chain: list[ET.Element],
               comment: str, context: list[str], address: str = ""):
        if len(self.tags) >= MAX_TAGS:
            self.warn_once("max", f"Tag limit of {MAX_TAGS} reached; remaining tags skipped")
            return
        type_name = type_name.strip()

        m = _ARRAY_RE.match(type_name)
        if m:
            self.struct_types[path] = type_name
            if comment:
                self.struct_comments[path] = comment
            self._expand_array(path, m[1], m[2].strip(), chain, comment, context)
            return

        members = self._members(type_name)
        if members is not None:
            self.struct_types[path] = type_name
            if comment:
                self.struct_comments[path] = comment
            sub_context = context + [comment] if comment and (not context or context[-1] != comment) else context
            for mv in members:
                name = mv.get("name")
                child_chain = [mv] + self._child_chain(chain, name)
                c = self._resolve_comment(child_chain, "")
                self.expand(f"{path}.{name}", mv.get("typeName"), child_chain, c, sub_context)
            return

        ti = type_info(type_name)
        if ti is None:
            if type_name in self.efbs:
                self.warn_once("efb:" + type_name, f"Skipped {path}: function block type {type_name} is not supported")
            else:
                self.warn_once("type:" + type_name, f"Skipped {path}: unknown type {type_name} (not defined in this file)")
            return

        literal = self._resolve_init(chain)
        initial = default_value(ti)
        if literal is not None:
            try:
                initial = parse_literal(literal, ti)
            except ValueError as e:
                self.warnings.append(f"{path}: initial value {literal!r} ignored ({e})")

        am = _ACCESS_RE.match(comment)
        if am:
            access = "R/" + am[1].upper()
            writable = access == "R/W"
        else:
            access, writable = "default", self.default_writable

        self.tags.append(Tag(path, ti.name, ti, initial, writable, access, comment,
                             [c for c in context if c != comment], address))

    def _expand_array(self, path, dims_text, elem_type, chain, comment, context):
        ranges = []
        for dim in dims_text.split(","):
            lo_hi = dim.split("..")
            try:
                lo, hi = int(lo_hi[0]), int(lo_hi[1])
            except (ValueError, IndexError):
                self.warn_once("dim:" + path, f"Skipped {path}: array bounds {dims_text!r} are not numeric")
                return
            ranges.append(range(lo, hi + 1))
        for idx in itertools.product(*ranges):
            key = "[" + ",".join(map(str, idx)) + "]"
            child_chain = self._child_chain(chain, key)
            c = self._resolve_comment(child_chain, comment)
            self.expand(path + key, elem_type, child_chain, c, context)


def parse_program(data: bytes, filename: str, default_writable: bool = True) -> Program:
    xef = _load_xef_bytes(data, filename)
    # Control Expert never emits entity declarations; refuse them rather than expand them.
    if b"<!ENTITY" in xef[:65536]:
        raise ParseError(f"{filename}: XML entity declarations are not allowed")
    try:
        root = ET.fromstring(xef)
    except ET.ParseError as e:
        raise ParseError(f"{filename}: not a valid XEF file ({e})") from e

    db = root.find("dataBlock")
    if db is None:
        raise ParseError(f"{filename}: no <dataBlock> section found")

    ch = root.find("contentHeader")
    fh = root.find("fileHeader")
    plc = root.find("IOConf/PLC/partItem")

    ex = _Expander(root, default_writable)
    hmi = 0
    for v in db.findall("variables"):
        if (_attr(v, "IsVariableHMI") or "").strip() != "-1":
            continue
        hmi += 1
        comment = (v.findtext("comment") or "").strip()
        ex.expand(v.get("name"), v.get("typeName") or "", [v], comment, [],
                  v.get("topologicalAddress") or "")

    return Program(
        name=(ch.get("name") if ch is not None else None) or "PLC",
        version=(ch.get("version") if ch is not None else "") or "",
        product=(fh.get("product") if fh is not None else "") or "",
        plc=(plc.get("partNumber") if plc is not None else "") or "",
        source_file=filename,
        tags=ex.tags,
        warnings=ex.warnings,
        hmi_variables=hmi,
        struct_comments=ex.struct_comments,
        struct_types=ex.struct_types,
    )

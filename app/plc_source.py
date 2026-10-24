"""Live tags from a PLC (or Control Expert PLC simulator) over UMAS.

The PLC data dictionary gives every exported variable with its type and memory offset;
structures are expanded into leaf tags named exactly like the export parser names them,
so comments and access from an optional .ZEF/.XEF can be overlaid by path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .iec_values import type_info
from .umas import TypeRecord, UmasClient, UmasConnectionError, UmasError
from .xef_parser import Program, Tag

log = logging.getLogger(__name__)

# UMAS elementary type ids -> (Control Expert type, byte size)
ELEMENTARY = {
    1: ("BOOL", 1), 2: ("BOOL", 1), 3: ("BOOL", 1), 4: ("INT", 2), 5: ("UINT", 2),
    6: ("DINT", 4), 7: ("UDINT", 4), 8: ("REAL", 4), 9: ("STRING", 16), 10: ("TIME", 4),
    14: ("DATE", 4), 15: ("TOD", 4), 16: ("DT", 8), 21: ("BYTE", 1), 22: ("WORD", 2),
    23: ("DWORD", 4), 25: ("EBOOL", 1),
}
_BY_NAME = {name: (tid, size) for tid, (name, size) in ELEMENTARY.items() if tid not in (2, 3)}
_STRUCT = {"INT": "<h", "UINT": "<H", "WORD": "<H", "DINT": "<i", "UDINT": "<I", "DWORD": "<I",
           "TIME": "<I", "REAL": "<f", "BYTE": "<B"}
_ARRAY_RE = re.compile(r"^ARRAY\s*\[(.+?)\]\s*OF\s+(.+)$", re.I)
_STRING_RE = re.compile(r"^STRING\[(\d+)\]$", re.I)
GAP_MERGE = 512           # read across gaps smaller than this (one request costs ~1 PLC cycle)


@dataclass
class Leaf:
    block: int
    offset: int
    size: int
    plc_type: str          # BOOL, EBOOL, INT, ..., STRING (size = max length)


class Layout:
    """Tag tree built from the PLC data dictionary."""

    def __init__(self, project: str, types: list[TypeRecord], variables, members: dict[int, list]):
        self.project = project
        self.types = {t.type_id: t for t in types}
        self.types_by_name = {t.name.upper(): t for t in types}
        self.members = members                     # type id -> [MemberRecord]
        self.tags: list[Tag] = []
        self.leaves: dict[str, Leaf] = {}
        self.warnings: list[str] = []
        self.struct_types: dict[str, str] = {}     # structure/array path -> type name
        for v in variables:
            self._expand(v.name, v.type_id, v.block, v.offset)
        self.ranges = self._plan_ranges()
        self.ordered = sorted(self.leaves.items(), key=lambda kv: (kv[1].block, kv[1].offset))

    # ---------------------------------------------------------------- expansion
    def _type_of(self, type_id: int):
        if type_id in ELEMENTARY:
            return ("elem",) + ELEMENTARY[type_id]
        t = self.types.get(type_id)
        if t is None:
            return None
        m = _STRING_RE.match(t.name)
        if m:
            return ("elem", "STRING", int(m[1]))
        if t.class_id == 2:
            return ("struct", t.name, t.size)
        if _ARRAY_RE.match(t.name):
            return ("array", t.name, t.size)
        return None

    def _elem_size(self, type_name: str) -> tuple[int, int] | None:
        """(type id, size) for an array element type given by name."""
        u = type_name.strip().upper()
        if u in _BY_NAME:
            return _BY_NAME[u]
        m = _STRING_RE.match(u)
        t = self.types_by_name.get(u)
        if t is not None:
            return t.type_id, t.size
        if m:
            return -int(m[1]), int(m[1])
        return None

    def _expand(self, path: str, type_id: int, block: int, offset: int):
        kind = self._type_of(type_id)
        if kind is None:
            self.warnings.append(f"Skipped {path}: unknown PLC data type id {type_id}")
            return
        if kind[0] == "elem":
            self._add_leaf(path, kind[1], block, offset, kind[2])
        elif kind[0] == "struct":
            self.struct_types[path] = kind[1]
            for m in self.members.get(type_id, []):
                self._expand(f"{path}.{m.name}", m.type_id, block, offset + m.offset)
        else:
            self.struct_types[path] = kind[1]
            self._expand_array(path, kind[1], block, offset)

    def _expand_array(self, path: str, type_name: str, block: int, offset: int):
        m = _ARRAY_RE.match(type_name)
        try:
            dims = [tuple(int(x) for x in d.split("..")) for d in m[1].split(",")]
        except ValueError:
            self.warnings.append(f"Skipped {path}: array bounds in {type_name!r} are not numeric")
            return
        elem = self._elem_size(m[2])
        if elem is None:
            self.warnings.append(f"Skipped {path}: unknown array element type {m[2]!r}")
            return
        elem_id, elem_size = elem
        idx = [lo for lo, _ in dims]
        n = 0
        while True:
            key = "[" + ",".join(map(str, idx)) + "]"
            off = offset + n * elem_size
            if elem_id < 0:
                self._add_leaf(path + key, "STRING", block, off, elem_size)
            else:
                self._expand(path + key, elem_id, block, off)
            n += 1
            for d in range(len(dims) - 1, -1, -1):      # row-major increment
                if idx[d] < dims[d][1]:
                    idx[d] += 1
                    break
                idx[d] = dims[d][0]
            else:
                break

    def _add_leaf(self, path: str, plc_type: str, block: int, offset: int, size: int):
        name = f"STRING[{size}]" if plc_type == "STRING" else plc_type
        ti = type_info(name)
        if ti is None:
            self.warnings.append(f"Skipped {path}: unsupported type {plc_type}")
            return
        writable = plc_type not in ("DATE", "TOD", "DT")
        self.tags.append(Tag(path, ti.name, ti, None, writable, "default", "", [], ""))
        self.leaves[path] = Leaf(block, offset, size, plc_type)

    def _plan_ranges(self) -> list[tuple[int, int, int]]:
        """Merge leaves into contiguous (block, start, length) spans."""
        spans: list[list[int]] = []
        for leaf in sorted(self.leaves.values(), key=lambda x: (x.block, x.offset)):
            end = leaf.offset + leaf.size
            if spans and spans[-1][0] == leaf.block and leaf.offset - spans[-1][2] <= GAP_MERGE:
                spans[-1][2] = max(spans[-1][2], end)
            else:
                spans.append([leaf.block, leaf.offset, end])
        return [(b, s, e - s) for b, s, e in spans]

    def program(self, host: str) -> Program:
        return Program(name=self.project or "PLC", version="", product=f"PLC at {host} (UMAS)",
                       plc="", source_file="", tags=self.tags, warnings=self.warnings,
                       hmi_variables=len({t.path.split(".")[0].split("[")[0] for t in self.tags}),
                       struct_types=dict(self.struct_types))

    # ---------------------------------------------------------------- values
    @staticmethod
    def decode(leaf: Leaf, data: bytes, pos: int):
        t = leaf.plc_type
        if t == "BOOL":
            return data[pos] != 0
        if t == "EBOOL":
            return bool(data[pos] & 0x01)
        if t in _STRUCT:
            return struct.unpack_from(_STRUCT[t], data, pos)[0]
        if t == "STRING":
            raw = data[pos:pos + leaf.size]
            return raw.split(b"\x00", 1)[0].decode("latin-1")
        if t == "DATE":
            d = data[pos:pos + 4]
            return f"{_bcd(d[3])}{_bcd(d[2]):02d}-{_bcd(d[1]):02d}-{_bcd(d[0]):02d}"
        if t == "TOD":
            d = data[pos:pos + 4]
            return f"{_bcd(d[3]):02d}:{_bcd(d[2]):02d}:{_bcd(d[1]):02d}.{_bcd(d[0]):02d}"
        if t == "DT":
            d = data[pos:pos + 8]
            return (f"{_bcd(d[7])}{_bcd(d[6]):02d}-{_bcd(d[5]):02d}-{_bcd(d[4]):02d} "
                    f"{_bcd(d[3]):02d}:{_bcd(d[2]):02d}:{_bcd(d[1]):02d}")
        raise ValueError(t)

    @staticmethod
    def encode(leaf: Leaf, value, current_byte: int | None = None) -> bytes:
        t = leaf.plc_type
        if t == "BOOL":
            return b"\x01" if value else b"\x00"
        if t == "EBOOL":
            # keep the other EBOOL status bits (history/forcing) as last read
            base = (current_byte or 0) & 0xFE
            return bytes([base | (1 if value else 0)])
        if t in _STRUCT:
            return struct.pack(_STRUCT[t], value)
        if t == "STRING":
            raw = value.encode("latin-1", "replace")[:leaf.size]
            return raw + b"\x00" * (leaf.size - len(raw))
        raise ValueError(f"writing {t} is not supported")


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


class PlcSource:
    """Keeps a UMAS connection, polls all tags and writes values back."""

    def __init__(self, host: str, port: int, unit: int, poll_interval: float, cache_dir: Path,
                 on_layout: Callable, on_values: Callable, on_state: Callable):
        self.host, self.port, self.unit = host, port, unit
        self.poll_interval = poll_interval
        self.cache_dir = cache_dir
        self.on_layout, self.on_values, self.on_state = on_layout, on_values, on_state
        self.client: UmasClient | None = None
        self.layout: Layout | None = None
        self.state = "connecting"
        self.error = ""
        self.scan_time = 0.0
        self.last_scan = 0.0
        self.crc = 0
        self._raw: dict[str, int] = {}             # EBOOL raw bytes from the last scan
        self._written_at: dict[str, float] = {}    # path -> time of our last write to it
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.client:
            await self.client.close()

    def _set_state(self, state: str, error: str = ""):
        if (state, error) != (self.state, self.error):
            self.state, self.error = state, error
            log.info("PLC %s: %s %s", self.address, state, error)
            self.on_state(state, error)

    # ---------------------------------------------------------------- main loop
    async def _run(self):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._set_state("connecting")
                self.client = UmasClient(self.host, self.port, self.unit)
                await self.client.connect()
                await self._load_layout()
                backoff = 2.0
                await self._poll_loop()
            except asyncio.CancelledError:
                raise
            except (UmasError, OSError) as e:
                self._set_state("disconnected", str(e))
            except Exception as e:
                log.exception("PLC source failed")
                self._set_state("disconnected", f"{type(e).__name__}: {e}")
            if self.client:
                await self.client.close()
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _load_layout(self):
        c = self.client
        self.crc = c.info.crc
        project = await c.project_name()
        cache = self.cache_dir / f"plc_dictionary_{c.info.crc:08x}.json"
        types = variables = members = None
        if cache.exists():
            try:
                types, variables, members = _load_cache(cache)
                log.info("Using cached PLC dictionary %s", cache.name)
            except Exception:
                log.warning("Ignoring unreadable dictionary cache %s", cache.name)
        if types is None:
            self._set_state("loading dictionary")
            t = time.time()
            types = await c.read_datatypes()
            variables = await c.read_variables()
            members = {}
            for ty in types:
                if ty.class_id == 2:
                    members[ty.type_id] = await c.read_members(ty.type_id)
            log.info("Read PLC dictionary: %d variables, %d types in %.1fs",
                     len(variables), len(types), time.time() - t)
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                _save_cache(cache, types, variables, members)
            except OSError as e:
                log.warning("Could not cache PLC dictionary: %s", e)
        self.layout = Layout(project, types, variables, members)
        values = await self.scan()
        await self.on_layout(self.layout, values)
        self._set_state("connected")

    async def _poll_loop(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.poll_interval)
            try:
                values = await self.scan()
            except UmasError as e:
                if isinstance(e, UmasConnectionError):
                    raise
                # a rejected read usually means the application changed (new CRC)
                if await self.client.read_status() != self.crc:
                    log.info("PLC application changed; reloading dictionary")
                    await self._load_layout()
                    continue
                raise
            await self.on_values(values)

    async def scan(self) -> dict[str, object]:
        """Read every tag; returns {path: value}."""
        t = time.time()
        c, layout = self.client, self.layout
        buffers: list[tuple[int, int, bytes]] = []
        for block, start, length in layout.ranges:
            pos = 0
            parts = []
            while pos < length:
                n = min(c.chunk_size, length - pos)
                parts.append(await c.read_bytes(block, start + pos, n))
                pos += n
            buffers.append((block, start, b"".join(parts)))
        values = {}
        bi = 0
        for path, leaf in layout.ordered:
            while not (buffers[bi][0] == leaf.block and buffers[bi][1] <= leaf.offset
                       < buffers[bi][1] + len(buffers[bi][2])):
                bi += 1
            block, start, data = buffers[bi]
            pos = leaf.offset - start
            values[path] = layout.decode(leaf, data, pos)
            if leaf.plc_type == "EBOOL":
                self._raw[path] = data[pos]
        # a scan that began before a write still holds the old value; drop it so a
        # just-written value is not briefly overwritten (it is picked up next scan)
        for path, written in list(self._written_at.items()):
            if written >= t:
                values.pop(path, None)
            else:
                del self._written_at[path]
        self.scan_time = time.time() - t
        self.last_scan = time.time()
        return values

    async def write(self, path: str, value):
        """Write one tag and return the value read back from the PLC."""
        if self.state != "connected" or not self.client:
            raise UmasConnectionError(f"PLC {self.address} is not connected")
        leaf = self.layout.leaves[path]
        data = self.layout.encode(leaf, value, self._raw.get(path))
        self._written_at[path] = time.time()
        await self.client.write_bytes(leaf.block, leaf.offset, data)
        raw = await self.client.read_bytes(leaf.block, leaf.offset, leaf.size)
        if leaf.plc_type == "EBOOL":
            self._raw[path] = raw[0]
        return self.layout.decode(leaf, raw, 0)


def _save_cache(path: Path, types, variables, members):
    path.write_text(json.dumps({
        "types": [[t.type_id, t.name, t.class_id, t.size] for t in types],
        "variables": [[v.name, v.type_id, v.block, v.offset] for v in variables],
        "members": {str(k): [[m.name, m.type_id, m.offset] for m in v] for k, v in members.items()},
    }))


def _load_cache(path: Path):
    from .umas import MemberRecord, VarRecord
    d = json.loads(path.read_text())
    types = [TypeRecord(*t) for t in d["types"]]
    variables = [VarRecord(*v) for v in d["variables"]]
    members = {int(k): [MemberRecord(*m) for m in v] for k, v in d["members"].items()}
    return types, variables, members

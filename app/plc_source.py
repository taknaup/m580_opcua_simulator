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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .iec_values import type_info
from .umas import TypeRecord, UmasClient, UmasConnectionError, UmasError, UmasNoDictionary
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
GAP_MERGE = 512           # background reads span gaps smaller than this (one request costs ~1 PLC cycle)


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


REF_BYTES = 9           # one byte-range reference in a UMAS read request
REQ_HEADER = 5          # CRC + reference count
WATCH_GAP = 16          # watched tags closer than this share one reference
HOT_SECONDS = 10.0      # a written tag stays watched this long (to see the logic react)
APP_CHECK_SECONDS = 5.0  # without a data dictionary: how often to check for a new download


@dataclass
class ReadRequest:
    segments: list[tuple[int, int, int]]            # (block, address, length)
    leaves: list[tuple[str, "Leaf", int]]           # (path, leaf, position in the reply)


def plan_reads(items, max_request: int, max_reply: int, gap: int) -> list[ReadRequest]:
    """Pack tags into as few UMAS read requests as possible.

    Tags closer than `gap` bytes share one byte-range reference; references are packed
    into a request while both the request and its reply fit in one frame. Each request
    costs about one PLC cycle, however many references it carries.
    """
    segs: list[list] = []                          # [block, start, end, [(path, leaf)]]
    for path, leaf in sorted(items, key=lambda kv: (kv[1].block, kv[1].offset)):
        end = leaf.offset + leaf.size
        if segs:
            last = segs[-1]
            if (last[0] == leaf.block and leaf.offset - last[2] <= gap
                    and max(last[2], end) - last[1] <= max_reply):
                last[2] = max(last[2], end)
                last[3].append((path, leaf))
                continue
        segs.append([leaf.block, leaf.offset, end, [(path, leaf)]])
    out: list[ReadRequest] = []
    reply = 0
    for block, start, end, members in segs:
        length = end - start
        cur = out[-1] if out else None
        if (cur is None or len(cur.segments) >= 255
                or REQ_HEADER + REF_BYTES * (len(cur.segments) + 1) > max_request
                or reply + length > max_reply):
            cur = ReadRequest([], [])
            out.append(cur)
            reply = 0
        for path, leaf in members:
            cur.leaves.append((path, leaf, reply + leaf.offset - start))
        cur.segments.append((block, start, length))
        reply += length
    return out


class PlcSource:
    """Keeps a UMAS connection, polls tags on demand and writes values back.

    Every `update_period` seconds the watched tags (from `watched()`: subscribed, read,
    on screen, or just written) are read, packed into as few requests as possible. The
    rest of the period reads the remaining memory in large chunks, round robin, so every
    tag is still refreshed; how often depends on project size, the watched set does not.
    """

    def __init__(self, host: str, port: int, unit: int, update_period: float, cache_dir: Path,
                 on_layout: Callable, on_values: Callable, on_state: Callable,
                 watched: Callable[[], set] | None = None):
        self.host, self.port, self.unit = host, port, unit
        self.update_period = update_period
        self.watched = watched or (lambda: set())
        self.cache_dir = cache_dir
        self.on_layout, self.on_values, self.on_state = on_layout, on_values, on_state
        self.client: UmasClient | None = None
        self.layout: Layout | None = None
        self.state = "connecting"
        self.error = ""
        self.scan_time = 0.0          # duration of the last cycle (watched + background)
        self.last_scan = 0.0
        self.crc = 0
        self.watched_count = 0
        self.watched_requests = 0
        self.background_requests = 0  # per cycle
        self.full_refresh = 0.0       # seconds for the background pass to cover every tag
        self._req_time = 0.1          # running average of one request, seconds
        self._background: list[ReadRequest] = []
        self._cursor = 0
        self._pass_started = 0.0
        self._watch_key: frozenset | None = None
        self._watch_plan: list[ReadRequest] = []
        # diagnostics
        self.umas_stats = {"requests": 0, "errors": 0, "seconds": 0.0}
        self.cycles: deque = deque(maxlen=180)    # (time, cycle s, watched reqs, background reqs)
        self.connected_since: float | None = None
        self.reconnects = 0
        self.mast_ms: int | None = None
        self.dictionary: dict = {}
        self._raw: dict[str, int] = {}             # EBOOL raw bytes from the last read
        self._written_at: dict[str, float] = {}    # path -> time of our last write to it
        self._hot: dict[str, float] = {}           # path -> time written (kept watched briefly)
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
            retry_now = False
            try:
                self._set_state("connecting")
                self.client = UmasClient(self.host, self.port, self.unit, stats=self.umas_stats)
                await self.client.connect()
                self.mast_ms = await self.client.mast_period_ms()
                await self._load_layout()
                if self.connected_since is not None:
                    self.reconnects += 1
                self.connected_since = time.time()
                backoff = 2.0
                await self._poll_loop()
            except asyncio.CancelledError:
                raise
            except UmasNoDictionary as e:
                # Reconnecting cannot help until a new application is downloaded: stay
                # connected and watch the application CRC instead.
                self._set_state("no dictionary", str(e))
                try:
                    retry_now = await self._wait_for_new_application()
                except (UmasError, OSError) as e2:
                    self._set_state("disconnected", str(e2))
            except (UmasError, OSError) as e:
                self._set_state("disconnected", str(e))
            except Exception as e:
                log.exception("PLC source failed")
                self._set_state("disconnected", f"{type(e).__name__}: {e}")
            if self.client:
                await self.client.close()
            if retry_now:
                backoff = 2.0
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _wait_for_new_application(self) -> bool:
        """Poll the application CRC; True once it changes (a new download), False on stop."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), APP_CHECK_SECONDS)
                return False
            except asyncio.TimeoutError:
                pass
            crc = await self.client.read_status()
            if crc != self.crc:
                log.info("PLC %s: application changed (CRC %08x -> %08x); retrying",
                         self.address, self.crc, crc)
                return True
        return False

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
                self.dictionary = {"source": "cache", "seconds": 0.0}
            except Exception:
                log.warning("Ignoring unreadable dictionary cache %s", cache.name)
        if types is None:
            try:
                await c.read_dictionary_id()
            except UmasNoDictionary:
                raise UmasNoDictionary(
                    f"PLC application {project or '(unnamed)'!r} has no embedded data dictionary. "
                    "In Control Expert enable Tools > Project Settings > General > PLC embedded data > "
                    "Data dictionary, then rebuild and download; the connection resumes by itself "
                    "after the download.") from None
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
            self.dictionary = {"source": "PLC", "seconds": round(time.time() - t, 1)}
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                _save_cache(cache, types, variables, members)
            except OSError as e:
                log.warning("Could not cache PLC dictionary: %s", e)
        self.layout = Layout(project, types, variables, members)
        self.dictionary.update(variables=len(variables), types=len(types), tags=len(self.layout.tags),
                               project=project)
        self._background = plan_reads(self.layout.leaves.items(), c.max_request, c.chunk_size, GAP_MERGE)
        self._cursor, self._watch_key, self._watch_plan = 0, None, []
        self._pass_started = time.time()
        values = await self.scan()
        await self.on_layout(self.layout, values)
        self._set_state("connected")

    async def _poll_loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                await self._cycle(t0)
            except UmasError as e:
                if isinstance(e, UmasConnectionError):
                    raise
                # a rejected read usually means the application changed (new CRC)
                if await self.client.read_status() != self.crc:
                    log.info("PLC application changed; reloading dictionary")
                    await self._load_layout()
                    continue
                raise
            await asyncio.sleep(max(0.0, self.update_period - (time.time() - t0)))

    def _watched_plan(self) -> list[ReadRequest]:
        now = time.time()
        for path, t in list(self._hot.items()):
            if now - t > HOT_SECONDS:
                del self._hot[path]
        leaves = self.layout.leaves
        key = frozenset(p for p in (self.watched() | set(self._hot)) if p in leaves)
        if key != self._watch_key:
            self._watch_key = key
            self._watch_plan = plan_reads(((p, leaves[p]) for p in key),
                                          self.client.max_request, self.client.chunk_size, WATCH_GAP)
        return self._watch_plan

    async def _cycle(self, t0: float):
        """One update period: all watched tags, then background chunks with the time left.

        Watched values are published as soon as they are read, before the background part.
        """
        values: dict[str, object] = {}
        plan = self._watched_plan()
        for req in plan:
            await self._execute(req, values)
        self._drop_stale(values, t0)
        await self.on_values(values)
        values = {}
        left = self.update_period - (time.time() - t0)
        n_bg = min(len(self._background), max(1, int(left / self._req_time)))
        for _ in range(n_bg):
            await self._execute(self._background[self._cursor], values)
            self._cursor += 1
            if self._cursor >= len(self._background):
                self._cursor = 0
                now = time.time()
                self.full_refresh = now - self._pass_started
                self._pass_started = now
        self._drop_stale(values, t0)
        await self.on_values(values)
        self.watched_count = len(self._watch_key or ())
        self.watched_requests, self.background_requests = len(plan), n_bg
        self.scan_time = time.time() - t0
        self.last_scan = time.time()
        self.cycles.append((self.last_scan, round(self.scan_time, 3), len(plan), n_bg))

    async def _execute(self, req: ReadRequest, values: dict):
        t = time.time()
        data = await self.client.read_segments(req.segments)
        self._req_time = 0.8 * self._req_time + 0.2 * (time.time() - t)
        decode = self.layout.decode
        for path, leaf, pos in req.leaves:
            values[path] = decode(leaf, data, pos)
            if leaf.plc_type == "EBOOL":
                self._raw[path] = data[pos]

    def _drop_stale(self, values: dict, started: float):
        # a read that began before a write still holds the old value; drop it so a
        # just-written value is not briefly overwritten (it is picked up next cycle)
        for path, written in list(self._written_at.items()):
            if written >= started:
                values.pop(path, None)
            else:
                del self._written_at[path]

    async def scan(self) -> dict[str, object]:
        """Read every tag once; returns {path: value}."""
        t = time.time()
        values: dict[str, object] = {}
        for req in self._background:
            await self._execute(req, values)
        self._drop_stale(values, t)
        self.scan_time = self.full_refresh = time.time() - t
        self.last_scan = time.time()
        return values

    async def read_now(self, paths: list[str]) -> dict[str, object]:
        """Read the given tags right away (packed like watched tags)."""
        if self.state != "connected" or not self.client:
            raise UmasConnectionError(f"PLC {self.address} is not connected")
        leaves = self.layout.leaves
        values: dict[str, object] = {}
        for req in plan_reads(((p, leaves[p]) for p in paths if p in leaves),
                              self.client.max_request, self.client.chunk_size, WATCH_GAP):
            await self._execute(req, values)
        return values

    async def write_many(self, items: list[tuple[str, object]]) -> int:
        """Write many tags, packed into as few requests as possible (one PLC cycle each).

        Values are not read back here; the tags stay watched for a while, so the next
        cycles read what the PLC actually holds.
        """
        if self.state != "connected" or not self.client:
            raise UmasConnectionError(f"PLC {self.address} is not connected")
        now = time.time()
        writes = []
        for path, value in items:
            leaf = self.layout.leaves[path]
            writes.append((leaf.block, leaf.offset, self.layout.encode(leaf, value, self._raw.get(path))))
            self._written_at[path] = self._hot[path] = now
        for batch in plan_writes(writes, self.client.max_request):
            await self.client.write_segments(batch)
        return len(writes)

    async def write(self, path: str, value):
        """Write one tag and return the value read back from the PLC."""
        if self.state != "connected" or not self.client:
            raise UmasConnectionError(f"PLC {self.address} is not connected")
        leaf = self.layout.leaves[path]
        data = self.layout.encode(leaf, value, self._raw.get(path))
        self._written_at[path] = self._hot[path] = time.time()
        await self.client.write_bytes(leaf.block, leaf.offset, data)
        raw = await self.client.read_bytes(leaf.block, leaf.offset, leaf.size)
        if leaf.plc_type == "EBOOL":
            self._raw[path] = raw[0]
        return self.layout.decode(leaf, raw, 0)


WRITE_REF_BYTES = 9     # block, address, length and flags of one write reference


def plan_writes(items: list[tuple[int, int, bytes]], max_request: int) -> list[list[tuple[int, int, bytes]]]:
    """Pack (block, address, data) writes into requests that fit in one frame."""
    out: list[list] = []
    size = 0
    for item in items:
        n = WRITE_REF_BYTES + len(item[2])
        if not out or len(out[-1]) >= 255 or size + n > max_request:
            out.append([])
            size = REQ_HEADER
        out[-1].append(item)
        size += n
    return out


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

"""Shared tag state between the OPC UA server, the web UI and the data source.

Two modes:
  export - tags and initial values come from an uploaded .ZEF/.XEF (offline simulation)
  plc    - tags and live values come from a PLC over UMAS; an uploaded export is optional
           and only overlays comments and R/O / R/W access
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path

from asyncua import ua

from .iec_values import coerce
from .opcua_server import OpcUaServer, WriteRejected
from .plc_source import Layout, PlcSource
from .snapshots import SnapshotError, SnapshotStore
from .umas import UmasConnectionError, UmasError
from .xef_parser import Program, Tag, parse_program

log = logging.getLogger(__name__)


HISTORY_PER_TAG = 240        # recent changes kept per tag for the detail panel
SAME_CHANGE_SECONDS = 3.0    # same value again within this time is the same change


def json_safe(v):
    """NaN/Infinity (possible in a live PLC REAL) are not valid JSON; send them as text."""
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if v != v else ("Infinity" if v > 0 else "-Infinity")
    return v


class TagReadOnly(Exception):
    pass


class PlcWriteFailed(Exception):
    pass


class NotAvailable(Exception):
    pass


class Simulator:
    def __init__(self, opcua: OpcUaServer, data_dir: Path, default_writable: bool, update_period: float = 1.0):
        self.opcua = opcua
        self.data_dir = data_dir
        self.default_writable = default_writable
        self.update_period = update_period
        self.ui_watch: dict[int, set[str]] = {}    # web UI connection -> tags on screen
        self.watch_stats: dict[str, int] = {}
        self.started = time.time()
        self.mode = "export"
        self.export: Program | None = None        # last uploaded .ZEF/.XEF
        self.program: Program | None = None       # what is being served
        self.plc: PlcSource | None = None
        self.layout: Layout | None = None
        self.overlay: dict = {}
        self.plc_defaults: dict = {}               # PLC_HOST etc., to pre-fill the PLC dialog
        self.tags: dict[str, Tag] = {}
        self.values: dict[str, object] = {}
        self.changed_at: dict[str, float] = {}
        self.sources: dict[str, str] = {}
        self.history: dict[str, deque] = {}        # path -> recent (time, value, source)
        self.snapshots = SnapshotStore(data_dir / "snapshots")
        self.generation = 0          # bumps whenever the tag set or its metadata changes
        self.loading = False         # startup program load in progress
        self._listeners: set = set()
        self._lock = asyncio.Lock()
        opcua.on_client_write = self._client_wrote

    # ---------------------------------------------------------------- listeners
    def add_listener(self, fn):
        self._listeners.add(fn)

    def remove_listener(self, fn):
        self._listeners.discard(fn)

    def _notify(self, event: dict):
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                log.exception("listener failed")

    def notify_status(self):
        self._notify({"type": "status"})

    def _reloaded(self):
        self.generation += 1
        # keep history only for tags that still exist
        for path in [p for p in self.history if p not in self.tags]:
            del self.history[path]
        self._notify({"type": "reload", "generation": self.generation})

    def _record(self, path: str, value, source: str, now: float | None = None):
        now = now or time.time()
        self.values[path] = value
        self.changed_at[path] = now
        self.sources[path] = source
        h = self.history.get(path)
        if h is None:
            h = self.history[path] = deque(maxlen=HISTORY_PER_TAG)
        if h and h[-1][1] == value and now - h[-1][0] < SAME_CHANGE_SECONDS:
            # the PLC poll saw a write before the writer recorded it: one change, keep the writer
            if h[-1][2] == "plc":
                h[-1] = (h[-1][0], value, source)
        else:
            h.append((now, value, source))
        self._notify({"type": "value", "path": path, "value": json_safe(value), "source": source, "ts": now})

    # ---------------------------------------------------------------- export files
    async def load_bytes(self, data: bytes, filename: str, persist: bool = True) -> Program:
        program = await asyncio.to_thread(parse_program, data, filename, self.default_writable)
        if persist:
            await asyncio.to_thread(self._persist, data, filename)
        self.export = program
        log.info("Loaded %s: %d HMI variables -> %d tags, %d warnings", filename,
                 program.hmi_variables, len(program.tags), len(program.warnings))
        if self.mode == "plc":
            await self._apply_overlay()
        else:
            await self._serve_export()
        return program

    async def _serve_export(self):
        async with self._lock:
            program = self.export
            if program is None:
                await self.opcua.clear()
                self.program, self.tags, self.values = None, {}, {}
            else:
                values = {t.path: t.initial for t in program.tags}
                await self.opcua.load(program, values)
                self.program = program
                self.tags = {t.path: t for t in program.tags}
                self.values = values
            self.changed_at, self.sources = {}, {}
        self._reloaded()

    async def remove_export(self):
        for name in ("program.bin", "program.json"):
            try:
                (self.data_dir / name).unlink()
            except FileNotFoundError:
                pass
        self.export = None
        if self.mode == "plc":
            await self._apply_overlay()
        else:
            await self._serve_export()

    def _persist(self, data: bytes, filename: str):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "program.bin").write_bytes(data)
        (self.data_dir / "program.json").write_text(json.dumps({"filename": filename}))

    async def load_persisted(self) -> bool:
        meta = self.data_dir / "program.json"
        blob = self.data_dir / "program.bin"
        if not (meta.exists() and blob.exists()):
            return False
        filename = json.loads(meta.read_text()).get("filename", "program")
        await self.load_bytes(blob.read_bytes(), filename, persist=False)
        return True

    # ---------------------------------------------------------------- PLC mode
    def last_plc(self) -> dict:
        """PLC last connected to from the web UI, else the configured default."""
        try:
            saved = json.loads((self.data_dir / "plc.json").read_text())
            if saved.get("host"):
                return {k: saved[k] for k in ("host", "port", "unit") if k in saved}
        except (FileNotFoundError, ValueError):
            pass
        return self.plc_defaults

    async def connect_plc(self, host: str, port: int = 502, unit: int = 1, persist: bool = True):
        await self._stop_plc()
        if persist:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "plc.json").write_text(json.dumps({"host": host, "port": port, "unit": unit}))
        async with self._lock:
            self.mode = "plc"
            self.layout = None
            # keep the nodes (and clients' subscriptions); they are reused for matching
            # tags once the PLC's tag list arrives
            await self.opcua.mark_all(ua.StatusCodes.BadWaitingForInitialData)
            self.program, self.tags, self.values = None, {}, {}
            self.changed_at, self.sources = {}, {}
        self.opcua.write_hook = self._opcua_write_to_plc
        self.plc = PlcSource(host, port, unit, self.update_period, self.data_dir,
                             self._on_layout, self._on_values, self._on_plc_state,
                             watched=self.watched_paths)
        self.plc.start()
        self._reloaded()

    async def disconnect_plc(self):
        await self._stop_plc()
        self.mode = "export"
        self.layout = None
        self.opcua.write_hook = None
        await self._serve_export()

    async def _stop_plc(self):
        if self.plc:
            plc, self.plc = self.plc, None
            await plc.stop()

    async def _on_layout(self, layout: Layout, values: dict):
        """Called by the PLC source after (re)connecting and reading all tags once."""
        same = (self.layout is not None and self.program is not None
                and [(t.path, t.type_name) for t in layout.tags] ==
                    [(t.path, t.type_name) for t in self.layout.tags])
        if same:
            # reconnect to the same application: keep nodes (and client subscriptions)
            self.layout = layout
            await self._on_values(values, force=True)
            return
        async with self._lock:
            self.layout = layout
            program = layout.program(self.plc.address)
            for t in program.tags:
                t.initial = values[t.path]
            self._overlay_tags(program)
            values = {t.path: values.get(t.path, t.initial) for t in program.tags}
            await self.opcua.load(program, values)
            self.program = program
            self.tags = {t.path: t for t in program.tags}
            self.values = dict(values)
            self.changed_at, self.sources = {}, {}
        self._reloaded()

    async def _on_values(self, values: dict, force: bool = False):
        now = time.time()
        for i, (path, v) in enumerate(values.items()):
            if path not in self.tags:
                continue
            if force or self.values.get(path) != v:
                await self.opcua.publish(path, v)
                if self.values.get(path) != v:
                    self._record(path, v, "plc", now)
            if i % 500 == 499:
                await asyncio.sleep(0)

    def _on_plc_state(self, state: str, error: str):
        if state == "disconnected" and self.tags:
            asyncio.create_task(self.opcua.mark_all(ua.StatusCodes.BadNotConnected))
        self.notify_status()

    def watched_paths(self) -> set[str]:
        """Tags to refresh every update period: OPC UA subscriptions/reads and web UI rows."""
        subscribed = self.opcua.subscribed_paths()
        read = self.opcua.read_paths()
        ui = set().union(*self.ui_watch.values()) if self.ui_watch else set()
        self.watch_stats = {"subscribed": len(subscribed), "read": len(read), "web_ui": len(ui)}
        return subscribed | read | ui

    def set_ui_watch(self, key: int, paths: set[str] | None):
        if paths is None:
            self.ui_watch.pop(key, None)
        else:
            self.ui_watch[key] = paths

    # ---------------------------------------------------------------- export overlay (PLC mode)
    def _overlay_tags(self, program: Program):
        """Apply comments and access from the uploaded export to PLC tags (in place)."""
        exp = {t.path.upper(): t for t in self.export.tags} if self.export else {}
        matched = 0
        for t in program.tags:
            base_writable = self.layout.leaves[t.path].plc_type not in ("DATE", "TOD", "DT")
            e = exp.get(t.path.upper())
            if e is not None:
                matched += 1
                t.comment, t.context, t.access, t.address = e.comment, list(e.context), e.access, e.address
                t.writable = base_writable and e.writable
            else:
                t.comment, t.context, t.access, t.address = "", [], "default", ""
                t.writable = base_writable
        program.struct_comments = dict(self.export.struct_comments) if self.export else {}
        plc_paths = {t.path.upper() for t in program.tags}
        self.overlay = {
            "file": self.export.source_file if self.export else None,
            "name": self.export.name if self.export else None,
            "version": self.export.version if self.export else None,
            "matched": matched,
            "plc_only": len(program.tags) - matched,
            "export_only": len(set(exp) - plc_paths),
            "name_mismatch": bool(self.export and self.export.name.upper() != program.name.upper()),
        }

    async def _apply_overlay(self):
        if self.program is None or self.layout is None:
            return          # applied when the PLC layout arrives
        async with self._lock:
            self._overlay_tags(self.program)
            await self.opcua.update_metadata(self.program.tags, self.program.struct_comments)
        self._reloaded()

    # ---------------------------------------------------------------- writes
    async def _write_plc(self, path: str, value):
        try:
            return await self.plc.write(path, value)
        except UmasConnectionError as e:
            raise PlcWriteFailed(f"PLC not connected: {e}") from e
        except UmasError as e:
            raise PlcWriteFailed(f"PLC rejected the write: {e}") from e

    async def _opcua_write_to_plc(self, path: str, value):
        if self.mode != "plc" or self.plc is None:
            return value
        try:
            return await self._write_plc(path, value)
        except PlcWriteFailed as e:
            status = ua.StatusCodes.BadNotConnected if "not connected" in str(e) else ua.StatusCodes.BadCommunicationError
            raise WriteRejected(status, str(e)) from e

    async def write_from_ui(self, path: str, raw):
        tag = self.tags.get(path)
        if tag is None:
            raise KeyError(path)
        if not tag.writable:
            raise TagReadOnly(path)
        value = coerce(raw, tag.ti)
        if self.mode == "plc":
            value = await self._write_plc(path, value)
        async with self._lock:
            await self.opcua.publish(path, value)
            self._record(path, value, "web")
        return value

    async def reset(self):
        if self.mode == "plc":
            raise NotAvailable("Reset is only available for an export file; in PLC mode the PLC owns the values")
        async with self._lock:
            for tag in self.tags.values():
                if self.values.get(tag.path) != tag.initial:
                    await self.opcua.publish(tag.path, tag.initial)
                    self._record(tag.path, tag.initial, "reset")

    # ---------------------------------------------------------------- snapshots
    def save_snapshot(self, name: str, paths: list[str] | None = None) -> dict:
        if not self.tags:
            raise SnapshotError("No tags loaded")
        chosen = self.tags if paths is None else [p for p in paths if p in self.tags]
        if not chosen:
            raise SnapshotError("None of the given tags exist")
        values = {p: json_safe(self.values[p]) for p in chosen}
        return self.snapshots.save(name, values, self.program.name if self.program else "", self.mode)

    async def apply_snapshot(self, snap_id: str) -> dict:
        """Write a snapshot's values; only writable tags whose value differs are written.

        In PLC mode the snapshot's tags are read from the PLC first, so "differs" is
        judged against what the PLC holds now, not the last poll.
        """
        doc = self.snapshots.load(snap_id)
        result = {"name": doc["name"], "written": 0, "unchanged": 0, "read_only": 0, "missing": 0, "invalid": 0}
        wanted: dict[str, object] = {}
        for path, raw in doc["values"].items():
            tag = self.tags.get(path)
            if tag is None:
                result["missing"] += 1
            elif not tag.writable:
                result["read_only"] += 1
            else:
                try:
                    wanted[path] = coerce(raw, tag.ti)
                except (ValueError, TypeError):
                    result["invalid"] += 1
        try:
            current = await self.plc.read_now(list(wanted)) if self.mode == "plc" else self.values
            todo = [(p, v) for p, v in wanted.items() if current.get(p) != v]
            if todo and self.mode == "plc":
                await self.plc.write_many(todo)
        except UmasConnectionError as e:
            raise PlcWriteFailed(f"PLC not connected: {e}") from e
        except UmasError as e:
            raise PlcWriteFailed(f"PLC rejected the write: {e}") from e
        result["unchanged"] = len(wanted) - len(todo)
        async with self._lock:
            now = time.time()
            for i, (path, value) in enumerate(todo):
                await self.opcua.publish(path, value)
                self._record(path, value, "snapshot", now)
                if i % 500 == 499:
                    await asyncio.sleep(0)
        result["written"] = len(todo)
        log.info("Applied snapshot %s: %s", doc["name"], result)
        return result

    def _client_wrote(self, path: str, value):
        if path in self.tags:
            self._record(path, value, "opcua")

    # ---------------------------------------------------------------- views
    def status(self) -> dict:
        p = self.program
        info = None
        if p:
            counts = {"R/O": 0, "R/W": 0, "default": 0}
            for t in p.tags:
                counts[t.access] += 1
            info = {
                "name": p.name, "version": p.version, "product": p.product, "plc": p.plc,
                "file": p.source_file, "hmi_variables": p.hmi_variables, "tags": len(p.tags),
                "access_counts": counts, "writable": sum(t.writable for t in p.tags),
                "warnings": p.warnings[:200], "warning_count": len(p.warnings),
            }
        plc = None
        if self.plc:
            plc = {"host": self.plc.host, "port": self.plc.port, "unit": self.plc.unit,
                   "state": self.plc.state, "error": self.plc.error,
                   "scan_time": round(self.plc.scan_time, 2), "last_scan": self.plc.last_scan,
                   "update_period": self.plc.update_period,
                   "watched": self.plc.watched_count, "watched_requests": self.plc.watched_requests,
                   "background_requests": self.plc.background_requests,
                   "full_refresh": round(self.plc.full_refresh, 1),
                   "watched_by": self.watch_stats,
                   "crc": f"{self.plc.crc:08x}" if self.plc.crc else None}
        export = None
        if self.export:
            export = {"file": self.export.source_file, "name": self.export.name,
                      "version": self.export.version, "tags": len(self.export.tags)}
        return {
            "mode": self.mode,
            "program": info,
            "plc": plc,
            "export": export,
            "overlay": self.overlay if self.mode == "plc" and self.export else None,
            "plc_last": self.last_plc(),
            "generation": self.generation,
            "loading": self.loading,
            "opcua": {"port": self.opcua.port, "sessions": self.opcua.session_count,
                      "namespace_index": self.opcua.ns, "root": p.name if p else None},
            "default_access": "R/W" if self.default_writable else "R/O",
        }

    def tag_detail(self, path: str) -> dict:
        """Everything about one tag, for the web UI's detail panel."""
        tag = self.tags[path]
        p = self.program
        from .opcua_server import NAMESPACE_URI, _prefixes, _split_path
        parents = [{"path": pre, "name": _split_path(pre)[-1],
                    "type": p.struct_types.get(pre, ""), "comment": p.struct_comments.get(pre, "")}
                   for pre in _prefixes(path)]
        plc = None
        if self.mode == "plc" and self.layout and path in self.layout.leaves:
            leaf = self.layout.leaves[path]
            plc = {"block": leaf.block, "offset": leaf.offset, "size": leaf.size, "type": leaf.plc_type,
                   "watched": bool(self.plc and path in (self.plc._watch_key or ()))}
        return {
            "path": path, "name": _split_path(path)[-1],
            "nodeid": f"ns={self.opcua.ns};s={path}", "nodeid_expanded": f"nsu={NAMESPACE_URI};s={path}",
            "type": tag.type_name, "ua_type": tag.ti.ua_type, "writable": tag.writable, "access": tag.access,
            "comment": tag.comment, "address": tag.address, "initial": json_safe(tag.initial),
            "value": json_safe(self.values.get(path)), "changed_at": self.changed_at.get(path),
            "source": self.sources.get(path), "parents": parents, "plc": plc,
            "subscribers": self.opcua.subscriber_count(path),
            "history": [[t, json_safe(v), s] for t, v, s in self.history.get(path, ())],
        }

    def diagnostics(self) -> dict:
        import importlib.metadata
        import platform
        p = self.program
        rss = None
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = round(int(line.split()[1]) / 1024, 1)
        except OSError:
            pass
        clients = self.opcua.clients()
        out = {
            "now": time.time(),
            "server": {
                "started": self.started, "mode": self.mode, "program": p.name if p else None,
                "file": self.export.source_file if self.export else None,
                "tags": len(self.tags), "memory_mb": rss,
                "history_entries": sum(len(h) for h in self.history.values()),
                "snapshots": len(self.snapshots.list()),
                "web_clients": len(self._listeners),
                "versions": {"python": platform.python_version(),
                             "asyncua": importlib.metadata.version("asyncua")},
            },
            "opcua": {
                "port": self.opcua.port, "clients": clients,
                "subscriptions": sum(c["subscriptions"] for c in clients),
                "monitored_items": sum(c["monitored_items"] for c in clients),
                "subscribed_tags": len(self.opcua.subscribed_paths()),
                "recently_read_tags": len(self.opcua.read_paths()),
            },
            "plc": None,
        }
        src = self.plc
        if src:
            st = src.umas_stats
            recent = list(src.cycles)[-30:]
            span = recent[-1][0] - recent[0][0] + recent[0][1] if len(recent) > 1 else 0
            out["plc"] = {
                "host": src.host, "port": src.port, "unit": src.unit, "state": src.state, "error": src.error,
                "connected_since": src.connected_since, "reconnects": src.reconnects,
                "plc_name": src.client.info.hostname if src.client else "",
                "max_frame": src.client.info.max_frame if src.client else None,
                "crc": f"{src.crc:08x}" if src.crc else None, "mast_ms": src.mast_ms,
                "dictionary": src.dictionary, "update_period": src.update_period,
                "cycle": round(src.scan_time, 2), "watched": src.watched_count, "watched_by": self.watch_stats,
                "watched_requests": src.watched_requests, "background_requests": src.background_requests,
                "full_refresh": round(src.full_refresh, 1),
                "requests": st["requests"], "errors": st["errors"],
                "avg_request_ms": round(1000 * st["seconds"] / st["requests"], 1) if st["requests"] else None,
                "requests_per_s": round(sum(c[2] + c[3] for c in recent) / span, 1) if span else None,
                "cycles": [[t, c, w, b] for t, c, w, b in src.cycles],
            }
        return out

    def folders(self) -> dict[str, list[str]]:
        """Structure/array paths -> [type name, comment], for the UI tree."""
        p = self.program
        if p is None:
            return {}
        keys = set(p.struct_types) | set(p.struct_comments)
        return {k: [p.struct_types.get(k, ""), p.struct_comments.get(k, "")] for k in keys}

    def tag_rows(self) -> list[list]:
        """Compact rows for the UI: [path, type, value, writable, access, comment, context, initial, address, ts, source]."""
        return [[t.path, t.type_name, json_safe(self.values[t.path]), t.writable, t.access, t.comment,
                 t.context, json_safe(t.initial), t.address, self.changed_at.get(t.path),
                 self.sources.get(t.path)] for t in self.tags.values()]

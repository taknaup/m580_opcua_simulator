"""Named sets of tag values, stored as JSON files in the data volume."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_SNAPSHOTS = 200
_ID_RE = re.compile(r"^[0-9]{14}-[a-z0-9-]{1,48}$")


class SnapshotError(Exception):
    pass


class SnapshotStore:
    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, snap_id: str) -> Path:
        if not _ID_RE.match(snap_id):
            raise SnapshotError("Invalid snapshot id")
        return self.dir / f"{snap_id}.json"

    def list(self) -> list[dict]:
        out = []
        if self.dir.exists():
            for f in sorted(self.dir.glob("*.json"), reverse=True):
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                out.append({k: d.get(k) for k in ("id", "name", "created", "project", "mode", "count", "note")})
        return out

    def save(self, name: str, values: dict, project: str, mode: str, note: str = "") -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise SnapshotError("Name must be 1-80 characters")
        if len(self.list()) >= MAX_SNAPSHOTS:
            raise SnapshotError(f"At most {MAX_SNAPSHOTS} snapshots; delete some first")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "snapshot"
        base = snap_id = f"{time.strftime('%Y%m%d%H%M%S')}-{slug}"
        n = 2
        while (self.dir / f"{snap_id}.json").exists():
            snap_id, n = f"{base}-{n}", n + 1
        doc = {"id": snap_id, "name": name, "created": time.time(), "project": project, "mode": mode,
               "note": note, "count": len(values), "values": values}
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(snap_id).write_text(json.dumps(doc))
        return doc

    def load(self, snap_id: str) -> dict:
        try:
            return json.loads(self._path(snap_id).read_text())
        except FileNotFoundError:
            raise SnapshotError("Snapshot not found") from None

    def delete(self, snap_id: str):
        try:
            self._path(snap_id).unlink()
        except FileNotFoundError:
            raise SnapshotError("Snapshot not found") from None

    def import_doc(self, raw: bytes, project: str, mode: str) -> dict:
        try:
            doc = json.loads(raw)
        except ValueError:
            raise SnapshotError("Not a JSON file") from None
        values = doc.get("values") if isinstance(doc, dict) else None
        if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
            raise SnapshotError('Expected {"name": ..., "values": {"tag.path": value, ...}}')
        return self.save(str(doc.get("name") or "Imported"), values, doc.get("project") or project,
                         doc.get("mode") or mode, note="imported")

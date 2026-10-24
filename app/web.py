"""HTTP API, WebSocket push and static UI."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .simulator import NotAvailable, PlcWriteFailed, Simulator, TagReadOnly, json_safe
from .snapshots import SnapshotError
from .xef_parser import ParseError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PUSH_INTERVAL = 0.15
MAX_UI_WATCH = 2000          # rows one browser may ask to keep fresh
MAX_SNAPSHOT_BYTES = 20 * 2**20


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,252})$|^\[?[0-9A-Fa-f:]+\]?$")


class WriteRequest(BaseModel):
    path: str
    value: object


class SnapshotRequest(BaseModel):
    name: str
    paths: list[str] | None = None       # None: every tag


class TrustRequest(BaseModel):
    status: str                          # "trusted" or "rejected"


_THUMB_RE = re.compile(r"^[0-9A-F]{40}$")


class PlcRequest(BaseModel):
    host: str
    port: int = 502
    unit: int = 1


def create_app(sim: Simulator, max_upload_bytes: int) -> FastAPI:
    app = FastAPI(title="M580 OPC UA Simulator", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"   # revalidate via ETag after upgrades
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    async def status(request: Request):
        s = sim.status()
        host = (request.headers.get("host") or "localhost").split(":")[0]
        s["opcua"]["endpoint_hint"] = f"opc.tcp://{host}:{sim.opcua.port}"
        return s

    @app.get("/api/tags")
    async def tags():
        return JSONResponse({"generation": sim.generation, "rows": sim.tag_rows(), "folders": sim.folders()})

    @app.get("/api/diagnostics")
    async def diagnostics():
        return sim.diagnostics()

    @app.get("/api/tag")
    async def tag_detail(path: str):
        if path not in sim.tags:
            raise HTTPException(404, f"Unknown tag {path}")
        return sim.tag_detail(path)

    @app.post("/api/tags/write")
    async def write(req: WriteRequest):
        try:
            value = await sim.write_from_ui(req.path, req.value)
        except KeyError:
            raise HTTPException(404, f"Unknown tag {req.path}")
        except TagReadOnly:
            raise HTTPException(403, f"{req.path} is read only (R/O)")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except PlcWriteFailed as e:
            raise HTTPException(502, str(e))
        return {"path": req.path, "value": json_safe(value)}

    @app.post("/api/reset")
    async def reset():
        try:
            await sim.reset()
        except NotAvailable as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    # ---------------------------------------------------------------- OPC UA security
    def _pki():
        pki = sim.opcua.pki
        if pki is None or not pki.cert_path.exists():
            raise HTTPException(404, "OPC UA security is not enabled (OPCUA_SECURITY is None only)")
        return pki

    def _thumb(thumb: str) -> str:
        t = thumb.upper()
        if not _THUMB_RE.match(t):
            raise HTTPException(422, "Invalid certificate thumbprint")
        return t

    @app.get("/api/security")
    async def security():
        pki = sim.opcua.pki
        secure = pki is not None and pki.cert_path.exists()
        return {
            "policies": sim.opcua.security,
            "client_certs": pki.client_mode if pki else None,
            "server_certificate": pki.server_cert_info() if secure else None,
            "clients": pki.clients() if secure else [],
        }

    @app.get("/api/security/server-cert.{fmt}")
    async def server_cert(fmt: str):
        pki = _pki()
        if fmt == "der":
            return Response(pki.cert_path.read_bytes(), media_type="application/pkix-cert",
                            headers={"Content-Disposition": 'attachment; filename="m580-opcua-simulator.der"'})
        if fmt == "pem":
            return Response(pki.server_cert_pem(), media_type="application/x-pem-file",
                            headers={"Content-Disposition": 'attachment; filename="m580-opcua-simulator.pem"'})
        raise HTTPException(404, "Use .der or .pem")

    @app.get("/api/security/clients/{thumb}.der")
    async def client_cert(thumb: str):
        try:
            der = _pki().client_der(_thumb(thumb))
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "Unknown client certificate")
        return Response(der, media_type="application/pkix-cert",
                        headers={"Content-Disposition": f'attachment; filename="{thumb.upper()}.der"'})

    @app.post("/api/security/clients/{thumb}")
    async def set_client_trust(thumb: str, req: TrustRequest):
        try:
            _pki().set_status(_thumb(thumb), req.status)
        except KeyError:
            raise HTTPException(404, "Unknown client certificate")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    @app.delete("/api/security/clients/{thumb}")
    async def remove_client_cert(thumb: str):
        try:
            _pki().remove(_thumb(thumb))
        except KeyError:
            raise HTTPException(404, "Unknown client certificate")
        return {"ok": True}

    # ---------------------------------------------------------------- snapshots
    @app.get("/api/snapshots")
    async def list_snapshots():
        return sim.snapshots.list()

    @app.post("/api/snapshots")
    async def save_snapshot(req: SnapshotRequest):
        try:
            doc = sim.save_snapshot(req.name, req.paths)
        except SnapshotError as e:
            raise HTTPException(422, str(e))
        return {k: v for k, v in doc.items() if k != "values"}

    @app.post("/api/snapshots/import")
    async def import_snapshot(file: UploadFile):
        raw = await file.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise HTTPException(413, "Snapshot file too large")
        try:
            doc = sim.snapshots.import_doc(raw, sim.program.name if sim.program else "", sim.mode)
        except SnapshotError as e:
            raise HTTPException(422, str(e))
        return {k: v for k, v in doc.items() if k != "values"}

    @app.get("/api/snapshots/{snap_id}")
    async def download_snapshot(snap_id: str):
        try:
            doc = sim.snapshots.load(snap_id)
        except SnapshotError as e:
            raise HTTPException(404, str(e))
        return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="{snap_id}.json"'})

    @app.post("/api/snapshots/{snap_id}/apply")
    async def apply_snapshot(snap_id: str):
        try:
            return await sim.apply_snapshot(snap_id)
        except SnapshotError as e:
            raise HTTPException(404, str(e))
        except PlcWriteFailed as e:
            raise HTTPException(502, str(e))

    @app.delete("/api/snapshots/{snap_id}")
    async def delete_snapshot(snap_id: str):
        try:
            sim.snapshots.delete(snap_id)
        except SnapshotError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.post("/api/plc")
    async def connect_plc(req: PlcRequest):
        host = req.host.strip()
        if not _HOST_RE.match(host):
            raise HTTPException(422, "Enter a host name or IP address")
        if not (1 <= req.port <= 65535) or not (0 <= req.unit <= 255):
            raise HTTPException(422, "Port must be 1-65535 and unit id 0-255")
        await sim.connect_plc(host, req.port, req.unit)
        return sim.status()

    @app.delete("/api/plc")
    async def disconnect_plc():
        await sim.disconnect_plc()
        return sim.status()

    @app.delete("/api/program")
    async def remove_program():
        await sim.remove_export()
        return sim.status()

    @app.post("/api/program")
    async def upload(file: UploadFile):
        name = Path(file.filename or "program").name
        if not name.lower().endswith((".zef", ".xef")):
            raise HTTPException(415, "Upload a .ZEF or .XEF file")
        data = await file.read(max_upload_bytes + 1)
        if len(data) > max_upload_bytes:
            raise HTTPException(413, f"File larger than {max_upload_bytes // 2**20} MB")
        try:
            await sim.load_bytes(data, name)
        except ParseError as e:
            raise HTTPException(422, str(e))
        return sim.status()

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        pending: dict[str, list] = {}
        wake = asyncio.Event()
        control: list[dict] = []

        def on_event(ev: dict):
            if ev["type"] == "value":
                pending[ev["path"]] = [ev["path"], ev["value"], ev["source"], ev["ts"]]
            else:
                pending.clear()
                control.append(ev)
            wake.set()

        sim.add_listener(on_event)

        async def pusher():
            last_sessions = None
            while True:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                wake.clear()
                while control:
                    await socket.send_json(control.pop(0))
                if pending:
                    batch = list(pending.values())
                    pending.clear()
                    await socket.send_json({"type": "values", "changes": batch})
                sessions = sim.opcua.session_count
                if sessions != last_sessions:
                    last_sessions = sessions
                    await socket.send_json({"type": "sessions", "count": sessions})
                await asyncio.sleep(PUSH_INTERVAL)

        task = asyncio.create_task(pusher())
        key = id(socket)
        try:
            while True:
                text = await socket.receive_text()
                if text.startswith("{"):
                    # {"type": "watch", "paths": [...]}: rows on screen, refreshed every period
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    if msg.get("type") == "watch" and isinstance(msg.get("paths"), list):
                        sim.set_ui_watch(key, {p for p in msg["paths"][:MAX_UI_WATCH] if isinstance(p, str)})
        except WebSocketDisconnect:
            pass
        finally:
            sim.set_ui_watch(key, None)
            sim.remove_listener(on_event)
            task.cancel()

    return app

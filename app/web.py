"""HTTP API, WebSocket push and static UI."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .simulator import NotAvailable, PlcWriteFailed, Simulator, TagReadOnly, json_safe
from .xef_parser import ParseError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PUSH_INTERVAL = 0.15
MAX_UI_WATCH = 2000          # rows one browser may ask to keep fresh


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,252})$|^\[?[0-9A-Fa-f:]+\]?$")


class WriteRequest(BaseModel):
    path: str
    value: object


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

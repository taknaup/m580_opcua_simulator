"""Entry point: OPC UA server and web UI on one asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from .opcua_server import OpcUaServer
from .simulator import Simulator
from .web import create_app


def env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


async def run():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("asyncua", "asyncua.server.address_space"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("main")

    default_access = env("DEFAULT_ACCESS", "RW").upper().replace("/", "")
    if default_access not in ("RW", "RO"):
        raise SystemExit("DEFAULT_ACCESS must be RW or RO")

    opcua = OpcUaServer(env("OPCUA_HOST", "0.0.0.0"), int(env("OPCUA_PORT", "4840")),
                        env("OPCUA_SERVER_NAME", "M580 OPC UA Simulator"), on_client_write=None)
    sim = Simulator(opcua, Path(env("DATA_DIR", "./data")), default_writable=default_access == "RW",
                    poll_interval=float(env("PLC_POLL_INTERVAL", "0.5")))
    await opcua.start()

    # Default: serve the export file. AUTOCONNECT=true connects to PLC_HOST at startup;
    # otherwise PLC_HOST only pre-fills the PLC dialog in the web UI.
    sim.plc_defaults = {"host": env("PLC_HOST", ""), "port": int(env("PLC_PORT", "502")),
                        "unit": int(env("PLC_UNIT", "1"))}
    autoconnect = env("AUTOCONNECT", "false").lower() in ("1", "true", "yes", "on")
    if autoconnect:
        if not sim.plc_defaults["host"]:
            raise SystemExit("AUTOCONNECT=true needs PLC_HOST")
        await sim.connect_plc(**sim.plc_defaults, persist=False)

    async def load_startup_program():
        sim.loading = sim.mode == "export"
        try:
            if not await sim.load_persisted():
                initial = env("PROGRAM_FILE", "")
                if initial and Path(initial).is_file():
                    await sim.load_bytes(Path(initial).read_bytes(), Path(initial).name)
        except Exception:
            log.exception("Could not load the saved program; upload one from the web UI")
        finally:
            sim.loading = False
            sim.notify_status()

    startup = asyncio.create_task(load_startup_program())  # noqa: F841 (keep a reference)

    app = create_app(sim, int(env("MAX_UPLOAD_MB", "100")) * 2**20)
    config = uvicorn.Config(app, host=env("WEB_HOST", "0.0.0.0"), port=int(env("WEB_PORT", "8000")),
                            proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
    try:
        await uvicorn.Server(config).serve()
    finally:
        await opcua.stop()


if __name__ == "__main__":
    asyncio.run(run())

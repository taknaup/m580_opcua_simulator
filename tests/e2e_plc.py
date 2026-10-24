"""End-to-end check of PLC (UMAS) mode against a running simulator and a PLC simulator.

Every value written is checked directly in PLC memory over UMAS and restored afterwards.

    docker run --rm --network host -v "$PWD/tests:/tests:ro" m580-opcua-sim:latest \
        python /tests/e2e_plc.py opc.tcp://127.0.0.1:4840 http://opcua-sim.localtest.me 192.168.0.128 \
        /tests/../HAC_SKID_v00_01_003.ZEF

Optional outage test: add  --relay 192.168.0.142:15020  (a host address the simulator container
can reach); a TCP relay on that port is started and cut to simulate losing the PLC.
"""
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/srv")
from asyncua import Client, ua  # noqa: E402

from app.plc_source import Layout  # noqa: E402
from app.umas import UmasClient  # noqa: E402

OPC, WEB, PLC_HOST, ZEF = sys.argv[1:5]
RELAY = sys.argv[sys.argv.index("--relay") + 1] if "--relay" in sys.argv else None
NS = "urn:m580-opcua-simulator:tags"
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def http(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(WEB + path, method=method, data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def upload(path):
    boundary = uuid.uuid4().hex
    blob = open(path, "rb").read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.split('/')[-1]}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + f"\r\n--{boundary}--\r\n".encode()
    return http("POST", "/api/program", raw=body, ctype=f"multipart/form-data; boundary={boundary}")


async def wait_status(pred, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        s = http("GET", "/api/status")[1]
        if pred(s):
            return s
        await asyncio.sleep(1)
    return http("GET", "/api/status")[1]


class Direct:
    """Reads PLC memory directly (independent of the simulator)."""

    def __init__(self):
        self.c = UmasClient(PLC_HOST, 502, unit=1)

    async def start(self):
        await self.c.connect()
        types = await self.c.read_datatypes()
        variables = await self.c.read_variables()
        members = {t.type_id: await self.c.read_members(t.type_id) for t in types if t.class_id == 2}
        self.layout = Layout("", types, variables, members)

    async def read(self, path):
        leaf = self.layout.leaves[path]
        return self.layout.decode(leaf, await self.c.read_bytes(leaf.block, leaf.offset, leaf.size), 0)

    async def write(self, path, value):
        leaf = self.layout.leaves[path]
        await self.c.write_bytes(leaf.block, leaf.offset, self.layout.encode(leaf, value))


async def wstatus(node, variant):
    try:
        await node.write_value(ua.DataValue(variant))
        return "Good"
    except ua.UaStatusCodeError as e:
        return type(e).__name__


class Sub:
    def __init__(self):
        self.q = asyncio.Queue()

    def datachange_notification(self, node, val, data):
        self.q.put_nowait((val, data.monitored_item.Value.StatusCode))


async def next_value(q, want, timeout):
    t = time.time()
    while time.time() - t < timeout:
        try:
            val, st = await asyncio.wait_for(q.get(), timeout - (time.time() - t))
        except asyncio.TimeoutError:
            break
        if want(val, st):
            return val, st, time.time() - t
    return None, None, None


DLY = "HAC_RPP2_BRKR07.StsAlrt.Dly"
SP = "HAC_IT_RACK11_BEV11_FLOW.MaxFlow.Sp"
MAN_AUTO = "HAC_RPP2_BRKR07.CntrlSel.ManAuto"
RO_TAG = "HAC_RPP2_BRKR07.StsAlrt.Alrt"
UNWATCHED = "HAC_RPP6_BRKR02.StsAlrt.Dly"              # not subscribed, read or written before the check


async def main():
    direct = Direct()
    await direct.start()
    orig = {p: await direct.read(p) for p in (DLY, SP, MAN_AUTO, UNWATCHED)}
    print("original PLC values:", orig, flush=True)

    start_mode = http("GET", "/api/status")[1]["mode"]
    if start_mode != "plc":
        http("POST", "/api/plc", {"host": PLC_HOST, "port": 502, "unit": 1})
    s = await wait_status(lambda s: s["mode"] == "plc" and s["plc"]["state"] == "connected" and s["program"])
    check(s["mode"] == "plc" and s["plc"]["state"] == "connected", f"simulator connected to PLC (scan {s['plc']['scan_time']}s)")
    check(s["program"]["tags"] == len(direct.layout.tags), f"serves all {len(direct.layout.tags)} PLC tags")

    client = Client(OPC)
    async with client:
        ns = await client.get_namespace_index(NS)
        n = lambda p: client.get_node(ua.NodeId(p, ns, ua.NodeIdType.String))  # noqa: E731
        try:
            # ---------------- without a comments file: everything writable
            http("DELETE", "/api/program")
            await asyncio.sleep(1)
            s = http("GET", "/api/status")[1]
            check(s["program"]["writable"] == s["program"]["tags"], "no comments file: all tags writable")
            ro = n(RO_TAG)
            al = (await ro.read_attribute(ua.AttributeIds.AccessLevel)).Value.Value
            check(bool(al & ua.AccessLevel.CurrentWrite.mask), "no comments file: AccessLevel has write bit")
            check((await ro.read_description()).Text == "", "no comments file: no Description")

            dly = n(DLY)
            check(await dly.read_value() == orig[DLY], f"OPC UA value equals PLC memory ({orig[DLY]})")
            check(await wstatus(dly, ua.Variant(30003, ua.VariantType.Int32)) == "Good", "OPC UA write DINT")
            check(await direct.read(DLY) == 30003, "DINT write landed in PLC memory")
            check(await wstatus(n(SP), ua.Variant(44.5, ua.VariantType.Float)) == "Good"
                  and await direct.read(SP) == 44.5, "REAL write landed in PLC memory")
            check(await wstatus(n(MAN_AUTO), ua.Variant(not orig[MAN_AUTO], ua.VariantType.Boolean)) == "Good"
                  and await direct.read(MAN_AUTO) == (not orig[MAN_AUTO]), "BOOL write landed in PLC memory")
            check(await wstatus(n(RO_TAG), ua.Variant(True, ua.VariantType.Boolean)) == "Good",
                  "no comments file: write accepted on a tag commented R/O")
            check(await wstatus(dly, ua.Variant(1.5, ua.VariantType.Double)) == "BadTypeMismatch",
                  "type checks still apply")

            # change in the PLC itself -> OPC UA subscription
            h = Sub()
            sub = await client.create_subscription(200, h)
            await sub.subscribe_data_change(dly)
            await asyncio.sleep(2)
            p = http("GET", "/api/status")[1]["plc"]
            cycle = max(p["update_period"], p["scan_time"])
            await direct.write(DLY, 30004)
            val, st, dt = await next_value(h.q, lambda v, st: v == 30004, 15)
            check(val == 30004 and dt <= 2 * cycle + 0.5,
                  f"PLC-side change reached OPC UA subscriber in {dt and round(dt, 1)}s "
                  f"(update cycle {cycle:.1f}s, {p['watched']} watched tags)")

            # ---------------- on-demand polling
            await on_demand_checks(client, n, direct)
            await snapshot_and_diagnostics_checks(direct)

            # writes landing mid-scan must not bounce back to the old value
            bounces = 0
            for i, v in enumerate((30101, 30102, 30103, 30104, 30105)):
                while not h.q.empty():
                    h.q.get_nowait()
                prev = await dly.read_value()
                await wstatus(dly, ua.Variant(v, ua.VariantType.Int32))
                seen = []
                t_end = time.time() + 5.5
                while time.time() < t_end:
                    try:
                        seen.append((await asyncio.wait_for(h.q.get(), max(0.05, t_end - time.time())))[0])
                    except asyncio.TimeoutError:
                        break
                if v in seen and prev in seen[seen.index(v):]:
                    bounces += 1
                    print(f"   bounce: wrote {v}, saw {seen}", flush=True)
                await asyncio.sleep(0.4 * i)       # vary where in the scan the next write lands
            check(bounces == 0, "no stale value after writes landing mid-scan (5 writes)")

            # web UI write -> PLC
            status, body = http("POST", "/api/tags/write", {"path": DLY, "value": 30005})
            check(status == 200 and await direct.read(DLY) == 30005, "web UI write landed in PLC memory")

            # ---------------- comments file uploaded: R/O enforced, subscription kept
            status, body = upload(ZEF)
            check(status == 200 and body["overlay"]["matched"] == body["program"]["tags"],
                  f"comments file applied ({body.get('overlay', {}).get('matched')} matched)")
            al = (await ro.read_attribute(ua.AttributeIds.AccessLevel)).Value.Value
            check(not (al & ua.AccessLevel.CurrentWrite.mask), "with comments file: R/O tag not writable")
            check((await ro.read_description()).Text.startswith("R/O"), "with comments file: Description is the comment")
            check(await wstatus(ro, ua.Variant(True, ua.VariantType.Boolean)) == "BadNotWritable",
                  "with comments file: write to R/O rejected")
            status, _ = http("POST", "/api/tags/write", {"path": RO_TAG, "value": True})
            check(status == 403, "with comments file: web write to R/O refused")
            await direct.write(DLY, 30006)
            val, st, dt = await next_value(h.q, lambda v, st: v == 30006, 15)
            check(val == 30006, "subscription survived applying the comments file")

            # ---------------- outage via relay
            if RELAY:
                await outage_test(client, ns, n, direct)
        finally:
            if RELAY:
                http("POST", "/api/plc", {"host": PLC_HOST, "port": 502, "unit": 1})
            for p, v in orig.items():
                await direct.write(p, v)
            restored = {p: await direct.read(p) for p in orig}
            check(restored == orig, f"original PLC values restored {restored}")
            if start_mode != "plc":
                http("DELETE", "/api/plc")          # back to file mode, as found

    await direct.c.close()
    print("\n%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


async def on_demand_checks(client, n, direct):
    s = http("GET", "/api/status")[1]["plc"]
    cycle = max(s["update_period"], s["scan_time"])
    full = max(s["full_refresh"], cycle)
    # an unwatched tag is still refreshed by the background pass
    t0 = time.time()
    await direct.write(UNWATCHED, 30013)
    seen = None
    while time.time() - t0 < full + cycle + 5:
        rows = http("GET", "/api/tags")[1]["rows"]
        seen = next(r[2] for r in rows if r[0] == UNWATCHED)
        if seen == 30013:
            break
        await asyncio.sleep(0.25)
    dt = time.time() - t0
    check(seen == 30013 and dt <= full + cycle + 2,
          f"unwatched tag refreshed by background pass in {dt:.1f}s (full refresh {s['full_refresh']}s)")
    # a client read makes the tag watched
    await n(UNWATCHED).read_value()
    await asyncio.sleep(cycle + 1)
    by = http("GET", "/api/status")[1]["plc"]["watched_by"]
    check(by.get("read", 0) >= 1, f"OPC UA read marks the tag watched ({by})")

    # load: 1,000 subscribed tags spread over the whole project
    rows = http("GET", "/api/tags")[1]["rows"]
    paths = [r[0] for r in rows][::max(1, len(rows) // 1000)][:1000]
    h = Sub()
    sub = await client.create_subscription(500, h)
    await sub.subscribe_data_change([n(p) for p in paths])
    await asyncio.sleep(6)
    s2 = http("GET", "/api/status")[1]["plc"]
    print(f"   load: {s2['watched']} watched tags -> {s2['watched_requests']} requests, "
          f"cycle {s2['scan_time']}s, full refresh {s2['full_refresh']}s", flush=True)
    check(s2["watched"] >= len(paths), f"{len(paths)} subscribed tags are watched")
    await sub.delete()


async def snapshot_and_diagnostics_checks(direct):
    # values changed directly in the PLC (the server may not have polled them yet)
    paths = [DLY, SP, MAN_AUTO]
    st, snap = http("POST", "/api/snapshots", {"name": "e2e plc", "paths": paths})
    saved = {p: (await direct.read(p)) for p in paths}
    await direct.write(DLY, 30031)
    await direct.write(SP, 47.25)
    st, r = http("POST", f"/api/snapshots/{snap['id']}/apply")
    check(st == 200 and r["written"] == 2 and r["unchanged"] == 1,
          f"snapshot apply in PLC mode compares fresh PLC values ({r})")
    check({p: await direct.read(p) for p in paths} == saved, "snapshot values landed in PLC memory")
    http("DELETE", f"/api/snapshots/{snap['id']}")
    d = http("GET", "/api/diagnostics")[1]["plc"]
    check(d["state"] == "connected" and d["mast_ms"] == 100 and d["requests"] > 0 and d["cycles"],
          f"diagnostics PLC section (MAST {d['mast_ms']} ms, {d['requests']} requests, avg {d['avg_request_ms']} ms)")


async def relay_server(host, port):
    async def handle(r1, w1):
        try:
            r2, w2 = await asyncio.open_connection(PLC_HOST, 502)
        except OSError:
            w1.close(); return

        async def pipe(r, w):
            try:
                while data := await r.read(65536):
                    w.write(data); await w.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                w.close()
        relay.conns += [w1, w2]
        await asyncio.gather(pipe(r1, w2), pipe(r2, w1))

    relay = await asyncio.start_server(handle, "0.0.0.0", port)
    relay.conns = []
    return relay


async def outage_test(client, ns, n, direct):
    host, port = RELAY.split(":")
    relay = await relay_server(host, int(port))
    node = n(DLY)
    h = Sub()
    sub = await client.create_subscription(200, h)
    await sub.subscribe_data_change(node)
    await asyncio.sleep(1)
    # switching the PLC connection reloads the tag list; nodes and subscriptions must survive
    http("POST", "/api/plc", {"host": host, "port": int(port), "unit": 1})
    s = await wait_status(lambda s: s["plc"]["state"] == "connected" and s["program"], 120)
    check(s["plc"]["state"] == "connected", "connected through relay (dictionary from cache)")
    await direct.write(DLY, 30012)
    val, st, dt = await next_value(h.q, lambda v, st: v == 30012, 15)
    check(val == 30012, "subscription made before switching PLC connection still updates")

    relay.close()
    for w in relay.conns:
        w.close()
    s = await wait_status(lambda s: s["plc"]["state"] == "disconnected", 30)
    check(s["plc"]["state"] == "disconnected", f"outage detected ({s['plc']['error'][:60]})")
    await asyncio.sleep(2)
    dv = await node.read_data_value(raise_on_bad_status=False)
    check(dv.StatusCode.value == ua.StatusCodes.BadNotConnected, f"value status during outage: {dv.StatusCode.name}")
    check(await wstatus(node, ua.Variant(30007, ua.VariantType.Int32)) in ("BadNotConnected", "BadCommunicationError"),
          "write during outage rejected")

    relay = await relay_server(host, int(port))
    s = await wait_status(lambda s: s["plc"]["state"] == "connected", 60)
    check(s["plc"]["state"] == "connected", "reconnected after outage")
    await asyncio.sleep(3)
    dv = await node.read_data_value()
    check(dv.StatusCode.is_good(), "value status Good after reconnect")
    await direct.write(DLY, 30008)
    val, st, dt = await next_value(h.q, lambda v, st: v == 30008, 15)
    check(val == 30008, "subscription survived the outage")
    relay.close()
    http("POST", "/api/plc", {"host": PLC_HOST, "port": 502, "unit": 1})
    await wait_status(lambda s: s["plc"]["host"] == PLC_HOST and s["plc"]["state"] == "connected" and s["program"], 120)


if __name__ == "__main__":
    asyncio.run(main())

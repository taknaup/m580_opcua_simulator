"""End-to-end check against a running simulator with the reference program loaded.

    docker run --rm --network proxy -v "$PWD/tests:/tests:ro" m580-opcua-sim:latest \
        python /tests/e2e_opcua.py opc.tcp://opcua-sim:4840 http://opcua-sim:8000
"""
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from asyncua import Client, ua

OPC, WEB = sys.argv[1], sys.argv[2]
NS = "urn:m580-opcua-simulator:tags"
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def http(method, path, body=None):
    req = urllib.request.Request(WEB + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def upload_json(path, doc):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"snap.json\"\r\n"
            f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(doc).encode() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(WEB + path, method="POST", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def ui_value(path):
    rows = http("GET", "/api/tags")[1]["rows"]
    row = next(r for r in rows if r[0] == path)
    return row[2], row[10]


async def write_status(node, variant):
    try:
        await node.write_value(ua.DataValue(variant))
        return "Good"
    except ua.UaStatusCodeError as e:
        return type(e).__name__


class Handler:
    def __init__(self):
        self.seen = asyncio.Queue()

    def datachange_notification(self, node, val, data):
        self.seen.put_nowait(val)


async def main():
    http("POST", "/api/reset")
    client = Client(OPC)
    eps = await client.connect_and_get_server_endpoints()
    check(all(e.EndpointUrl.startswith(OPC) for e in eps), f"endpoints echo client URL ({eps[0].EndpointUrl})")

    async with client:
        ns = await client.get_namespace_index(NS)
        n = lambda p: client.get_node(ua.NodeId(p, ns, ua.NodeIdType.String))  # noqa: E731

        root = n("HAC_SKID_v00_01")
        check(len(await root.get_children()) == 1806, "project folder has 1806 HMI variables")

        dly = n("HAC_RPP2_BRKR07.StsAlrt.Dly")
        check(await dly.read_value() == 30000, "initial value from instance override (30000)")
        check((await dly.read_description()).Text == "R/W - Delay (mSeconds)", "comment served as Description")
        check(await dly.read_data_type() == ua.NodeId(ua.ObjectIds.Int32), "DINT -> Int32")
        brkr = n("HAC_RPP2_BRKR07")
        check((await brkr.read_description()).Text == "Remote Power Panel 2 Breaker 7", "structure comment on folder")

        check(await write_status(dly, ua.Variant(12345, ua.VariantType.Int32)) == "Good", "write R/W tag")
        check(ui_value("HAC_RPP2_BRKR07.StsAlrt.Dly") == [12345, "opcua"] or
              ui_value("HAC_RPP2_BRKR07.StsAlrt.Dly") == (12345, "opcua"), "client write visible in web API")

        opn = n("HAC_RPP2_BRKR07.OpnCmd")
        al = await opn.read_attribute(ua.AttributeIds.AccessLevel)
        check(not (al.Value.Value & ua.AccessLevel.CurrentWrite.mask), "R/O tag AccessLevel has no write bit")
        check(await write_status(opn, ua.Variant(True, ua.VariantType.Boolean)) in
              ("BadNotWritable", "BadUserAccessDenied"), "write to R/O tag rejected")
        check(await opn.read_value() is False, "R/O tag unchanged")

        io_ch = n("CPU_r0_s0_d01_c00m01_c0")   # INT, comment has no prefix -> default R/W
        check(await write_status(io_ch, ua.Variant(1.5, ua.VariantType.Double)) == "BadTypeMismatch", "fractional to INT rejected")
        check(await write_status(io_ch, ua.Variant(70000, ua.VariantType.Int32)) == "BadOutOfRange", "out-of-range INT rejected")
        check(await write_status(io_ch, ua.Variant(-123, ua.VariantType.Int16)) == "Good", "unprefixed tag writable")
        check(await write_status(io_ch, ua.Variant("x", ua.VariantType.String)) == "BadTypeMismatch", "string to INT rejected")

        # subscription receives a change made in the web UI
        man = n("HAC_RPP2_BRKR07.CntrlSel.ManCmd")
        h = Handler()
        sub = await client.create_subscription(100, h)
        await sub.subscribe_data_change(man)
        first = await asyncio.wait_for(h.seen.get(), 5)
        status, _ = http("POST", "/api/tags/write", {"path": "HAC_RPP2_BRKR07.CntrlSel.ManCmd", "value": True})
        check(status == 200, "web write to R/W tag accepted")
        got = await asyncio.wait_for(h.seen.get(), 5)
        check(first is False and got is True, "OPC UA subscription sees web UI write")
        await sub.delete()

        status, body = http("POST", "/api/tags/write", {"path": "HAC_RPP2_BRKR07.OpnCmd", "value": True})
        check(status == 403, f"web write to R/O tag refused ({body.get('detail')})")
        status, body = http("POST", "/api/tags/write", {"path": "HAC_RPP2_BRKR07.StsAlrt.Dly", "value": "abc"})
        check(status == 422, f"web write with bad value refused ({body.get('detail')})")
        status, _ = http("POST", "/api/tags/write", {"path": "HAC_RPP2_BRKR07.StsAlrt.Dly", "value": "16#FF"})
        check(status == 200 and await dly.read_value() == 255, "web write with IEC literal")

        # ---------------- tag detail
        st, d = http("GET", "/api/tag?path=" + urllib.parse.quote("HAC_RPP2_BRKR07.StsAlrt.Dly"))
        check(st == 200 and d["nodeid"] == f"ns={ns};s=HAC_RPP2_BRKR07.StsAlrt.Dly", f"tag detail NodeId ({d.get('nodeid')})")
        check([p["type"] for p in d["parents"]] == ["dev_BRKR_h01", "sfw_Alert_f02"], "tag detail structure chain with DDT types")
        check([h[1:] for h in d["history"]][-2:] == [[12345, "opcua"], [255, "web"]], f"tag detail history ({d['history'][-2:]})")
        check(http("GET", "/api/tag?path=nope")[0] == 404, "unknown tag detail is 404")

        # ---------------- snapshots
        paths = ["HAC_RPP2_BRKR07.StsAlrt.Dly", "HAC_RPP2_BRKR07.CntrlSel.ManCmd", "HAC_RPP2_BRKR07.OpnCmd"]
        st, snap = http("POST", "/api/snapshots", {"name": "e2e scenario", "paths": paths})
        check(st == 200 and snap["count"] == 3, "snapshot of 3 tags saved")
        http("POST", "/api/tags/write", {"path": paths[0], "value": 111})
        http("POST", "/api/tags/write", {"path": paths[1], "value": False})
        st, r = http("POST", f"/api/snapshots/{snap['id']}/apply")
        check(st == 200 and (r["written"], r["unchanged"], r["read_only"]) == (2, 0, 1),
              f"snapshot apply writes changed R/W tags, skips R/O ({r})")
        check(await dly.read_value() == 255 and await man.read_value() is True, "snapshot values back on OPC UA")
        st, full = http("GET", f"/api/snapshots/{snap['id']}")
        check(st == 200 and full["values"][paths[0]] == 255, "snapshot download has values")
        st, imp = upload_json("/api/snapshots/import", {"name": "imported", "values": full["values"]})
        check(st == 200 and imp["count"] == 3 and imp["note"] == "imported", "snapshot import")
        check(upload_json("/api/snapshots/import", {"values": [1]})[0] == 422, "bad snapshot file rejected")
        for sid in (snap["id"], imp["id"]):
            http("DELETE", f"/api/snapshots/{sid}")
        check(not [x for x in http("GET", "/api/snapshots")[1] if x["id"] in (snap["id"], imp["id"])], "snapshots deleted")

        # ---------------- diagnostics
        sub2 = await client.create_subscription(250, Handler())
        await sub2.subscribe_data_change([dly, man])
        diag = http("GET", "/api/diagnostics")[1]
        mine = [c for c in diag["opcua"]["clients"] if c["monitored_items"] >= 2]
        check(len(mine) >= 1 and 250 in mine[0]["publishing_ms"], f"diagnostics lists this client ({mine[:1]})")
        check(diag["server"]["tags"] == 6050 and diag["plc"] is None, "diagnostics server section (export mode)")
        await sub2.delete()

        sessions = http("GET", "/api/status")[1]["opcua"]["sessions"]
        check(sessions >= 1, f"status reports {sessions} OPC UA session(s)")

        http("POST", "/api/reset")
        check(await dly.read_value() == 30000 and await man.read_value() is False, "reset restores initial values")

    print("\n%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


asyncio.run(main())

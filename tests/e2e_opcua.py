"""End-to-end check against a running simulator with the reference program loaded.

    docker run --rm --network proxy -v "$PWD/tests:/tests:ro" m580-opcua-sim:latest \
        python /tests/e2e_opcua.py opc.tcp://opcua-sim:4840 http://opcua-sim:8000
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request

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

        sessions = http("GET", "/api/status")[1]["opcua"]["sessions"]
        check(sessions >= 1, f"status reports {sessions} OPC UA session(s)")

        http("POST", "/api/reset")
        check(await dly.read_value() == 30000 and await man.read_value() is False, "reset restores initial values")

    print("\n%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


asyncio.run(main())

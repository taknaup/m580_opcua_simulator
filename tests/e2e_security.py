"""End-to-end OPC UA security check against a running simulator with a program loaded.

    docker run --rm --network host -v "$PWD/tests:/tests:ro" m580-opcua-sim:latest \
        python /tests/e2e_security.py opc.tcp://<host>:4840 http://<host>:8000 trusted

Last argument: the server's configuration under test
    trusted   OPCUA_SECURITY=all,    OPCUA_CLIENT_CERTS=trusted
    accept    OPCUA_SECURITY=secure, OPCUA_CLIENT_CERTS=accept
Client certificates it creates are removed from the server again at the end.
"""
import asyncio
import hashlib
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.x509.oid import ExtendedKeyUsageOID

from asyncua import Client, ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.security_policies import (SecurityPolicyAes128Sha256RsaOaep, SecurityPolicyAes256Sha256RsaPss,
                                              SecurityPolicyBasic256Sha256)

OPC, WEB, CONFIG = sys.argv[1:4]
NS = "urn:m580-opcua-simulator:tags"
TAG = "HAC_RPP2_BRKR07.StsAlrt.Dly"
APP_URI = "urn:e2e:secure-client"
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def http(method, path, body=None, raw=False):
    req = urllib.request.Request(WEB + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data))
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def session(policy=None, mode=ua.MessageSecurityMode.SignAndEncrypt, cert=None, key=None, write=None):
    """Connect, read the tag (optionally write it); return (value, None) or (None, error name)."""
    c = Client(OPC, timeout=8)
    c.application_uri = APP_URI
    c.name = "E2E secure client"
    if policy is not None:
        await c.set_security(policy, str(cert), str(key), mode=mode)
    try:
        async with c:
            ns = await c.get_namespace_index(NS)
            node = c.get_node(ua.NodeId(TAG, ns, ua.NodeIdType.String))
            if write is not None:
                await node.write_value(ua.DataValue(ua.Variant(write, ua.VariantType.Int32)))
            return await node.read_value(), None
    except Exception as e:                       # noqa: BLE001 - report what the server said
        return None, type(e).__name__


def thumb(der: bytes) -> str:
    return hashlib.sha1(der).hexdigest().upper()


async def main():
    tmp = Path(tempfile.mkdtemp())
    cert, key = tmp / "client.der", tmp / "client.pem"
    await setup_self_signed_certificate(key, cert, APP_URI, "e2e-client", [ExtendedKeyUsageOID.CLIENT_AUTH],
                                        {"organizationName": "E2E"})
    my_thumb = thumb(cert.read_bytes())

    # endpoints and server certificate
    eps = await Client(OPC).connect_and_get_server_endpoints()
    offered = {(e.SecurityPolicyUri.rsplit("#", 1)[-1], e.SecurityMode) for e in eps}
    has_none = ("None", ua.MessageSecurityMode.None_) in offered
    st, der = http("GET", "/api/security/server-cert.der", raw=True)
    secure_eps = [e for e in eps if e.SecurityMode != ua.MessageSecurityMode.None_]
    check(st == 200 and all(thumb(e.ServerCertificate) == thumb(der) for e in secure_eps),
          f"server certificate on the endpoints matches the download ({thumb(der)[:8]}…)")
    if CONFIG == "trusted":
        check(len(offered) == 7 and has_none, f"all 7 endpoints offered ({len(offered)})")
    else:
        check(len(offered) == 6 and not has_none, f"6 secure endpoints, None not offered ({len(offered)})")
    check(all(e.EndpointUrl.startswith(OPC.rstrip("/")) for e in eps), "endpoint URLs echo the client's URL")

    # unencrypted
    val, err = await session()
    if CONFIG == "trusted":
        check(err is None and val is not None, f"None connection without a certificate reads {val}")
    else:
        check(err is not None, f"None connection refused ({err})")

    b256 = SecurityPolicyBasic256Sha256
    if CONFIG == "trusted":
        val, err = await session(b256, cert=cert, key=key)
        check(err == "BadCertificateUntrusted", f"unknown client certificate refused ({err})")
        rec = next((c for c in http("GET", "/api/security")[1]["clients"] if c["thumbprint"] == my_thumb), None)
        check(rec is not None and rec["status"] == "rejected" and rec["application"] == "E2E secure client",
              f"refused certificate listed for approval ({rec and rec['status']})")
        st, _ = http("POST", f"/api/security/clients/{my_thumb}", {"status": "trusted"})
        check(st == 200, "certificate trusted through the API")

    original, _ = await session(b256, cert=cert, key=key)
    val, err = await session(b256, cert=cert, key=key, write=31234)
    check(err is None and val == 31234, f"Basic256Sha256 Sign & Encrypt: write and read back ({val}, {err})")
    val, err = await session(SecurityPolicyAes256Sha256RsaPss, cert=cert, key=key)
    check(err is None and val == 31234, f"Aes256Sha256RsaPss Sign & Encrypt ({err or 'ok'})")
    val, err = await session(SecurityPolicyAes128Sha256RsaOaep, ua.MessageSecurityMode.Sign, cert=cert, key=key)
    check(err is None and val == 31234, f"Aes128Sha256RsaOaep Sign ({err or 'ok'})")
    await session(b256, cert=cert, key=key, write=original)

    # per-tag rules still apply on secure channels
    c = Client(OPC, timeout=8)
    c.application_uri = APP_URI
    await c.set_security(b256, str(cert), str(key), mode=ua.MessageSecurityMode.SignAndEncrypt)
    async with c:
        ns = await c.get_namespace_index(NS)
        ro = c.get_node(ua.NodeId("HAC_RPP2_BRKR07.OpnCmd", ns, ua.NodeIdType.String))
        try:
            await ro.write_value(ua.DataValue(ua.Variant(True, ua.VariantType.Boolean)))
            err = None
        except ua.UaStatusCodeError as e:
            err = type(e).__name__
        check(err == "BadNotWritable", f"R/O tag still refuses writes over Sign & Encrypt ({err})")

    # the security column in diagnostics, seen while connected
    c = Client(OPC, timeout=8)
    c.application_uri = APP_URI
    await c.set_security(b256, str(cert), str(key), mode=ua.MessageSecurityMode.SignAndEncrypt)
    async with c:
        clients = http("GET", "/api/diagnostics")[1]["opcua"]["clients"]
        check(any(x["security"] == "Basic256Sha256 · Sign & Encrypt" for x in clients),
              f"diagnostics shows the channel security ({[x['security'] for x in clients]})")

    rec = next(c for c in http("GET", "/api/security")[1]["clients"] if c["thumbprint"] == my_thumb)
    expected = "trusted" if CONFIG == "trusted" else "auto"
    check(rec["status"] == expected and rec["uri_matches"] and rec["last_mode"] == "SignAndEncrypt",
          f"client certificate record: {rec['status']}, URI matches, last mode {rec['last_mode']}")

    http("POST", f"/api/security/clients/{my_thumb}", {"status": "rejected"})
    val, err = await session(b256, cert=cert, key=key)
    check(err == "BadCertificateUntrusted", f"rejected certificate refused again ({err})")
    st, _ = http("DELETE", f"/api/security/clients/{my_thumb}")
    check(st == 200 and not [c for c in http("GET", "/api/security")[1]["clients"] if c["thumbprint"] == my_thumb],
          "certificate removed from the list")

    print("\n%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

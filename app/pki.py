"""OPC UA application certificate and client certificate trust.

Files under <data>/pki:
  own/cert.der, own/key.pem   the server's self-signed application certificate
  clients.json                client certificates seen, with their trust status
  clients/<thumbprint>.der    those certificates

Client trust modes:
  accept   any valid client certificate is accepted on signed/encrypted channels and
           recorded as "auto"; one marked "rejected" is refused
  trusted  only certificates marked "trusted" are accepted; an unknown certificate is
           refused and recorded as "rejected" until someone trusts it
Unencrypted (None) channels don't authenticate the client, so no trust check applies.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from contextvars import ContextVar
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from asyncua import ua
from asyncua.common.utils import ServiceError
from asyncua.crypto.permission_rules import PermissionRuleset
from asyncua.server.users import UserRole
from asyncua.crypto.cert_gen import (dump_private_key_as_pem, generate_private_key,
                                     generate_self_signed_app_certificate)

log = logging.getLogger(__name__)

POLICY_NONE_URI = "http://opcfoundation.org/UA/SecurityPolicy#None"
CERT_DAYS = 5 * 365
RENEW_BEFORE_DAYS = 30
STATUSES = ("trusted", "rejected", "auto")

# security policy URI and mode of the channel the current request arrived on; set per
# request by the OPC UA server (asyncua doesn't pass it to the certificate validator)
channel_security: ContextVar[tuple[str, int]] = ContextVar("channel_security", default=(POLICY_NONE_URI, 1))

POLICIES = {
    "None": ua.SecurityPolicyType.NoSecurity,
    "Basic256Sha256_Sign": ua.SecurityPolicyType.Basic256Sha256_Sign,
    "Basic256Sha256_SignAndEncrypt": ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
    "Aes128Sha256RsaOaep_Sign": ua.SecurityPolicyType.Aes128Sha256RsaOaep_Sign,
    "Aes128Sha256RsaOaep_SignAndEncrypt": ua.SecurityPolicyType.Aes128Sha256RsaOaep_SignAndEncrypt,
    "Aes256Sha256RsaPss_Sign": ua.SecurityPolicyType.Aes256Sha256RsaPss_Sign,
    "Aes256Sha256RsaPss_SignAndEncrypt": ua.SecurityPolicyType.Aes256Sha256RsaPss_SignAndEncrypt,
}
SECURE = [k for k in POLICIES if k != "None"]


class SimulatorRuleset(PermissionRuleset):
    """Service permissions on signed/encrypted endpoints.

    asyncua's default ruleset there lets only Admin users write and blocks services such
    as BrowseNext and RegisterNodes. Behave like the None endpoint instead: every service
    is allowed (tag writes are still checked per tag for R/O, type and range), except
    changing the address space or registering servers, which stays admin-only.
    """
    ADMIN_ONLY = {ua.NodeId(i) for i in (
        ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.DeleteNodesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.AddReferencesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.DeleteReferencesRequest_Encoding_DefaultBinary,
        ua.ObjectIds.RegisterServerRequest_Encoding_DefaultBinary,
        ua.ObjectIds.RegisterServer2Request_Encoding_DefaultBinary,
    )}

    def check_validity(self, user, action_type_id, body):
        return user.role == UserRole.Admin or action_type_id not in self.ADMIN_ONLY


def parse_policies(text: str) -> list[str]:
    """OPCUA_SECURITY: "all" (default), "secure" (all but None), or a comma list of names."""
    t = (text or "all").strip()
    if t.lower() == "all":
        return list(POLICIES)
    if t.lower() == "secure":
        return list(SECURE)
    names = [p.strip() for p in t.split(",") if p.strip()]
    lookup = {k.lower(): k for k in POLICIES}
    bad = [n for n in names if n.lower() not in lookup]
    if bad or not names:
        raise ValueError(f"OPCUA_SECURITY: unknown policy {bad or text!r}; choose from all, secure, "
                         + ", ".join(POLICIES))
    return [lookup[n.lower()] for n in names]


def thumbprint(der: bytes) -> str:
    """SHA-1 of the DER certificate, as OPC UA tools show it."""
    return hashlib.sha1(der).hexdigest().upper()


def _cn(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else cert.subject.rfc4514_string()


def _san(cert: x509.Certificate) -> tuple[list[str], list[str], list[str]]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], [], []
    return ([str(u) for u in ext.get_values_for_type(x509.UniformResourceIdentifier)],
            [str(d) for d in ext.get_values_for_type(x509.DNSName)],
            [str(i) for i in ext.get_values_for_type(x509.IPAddress)])


def _validity(cert: x509.Certificate) -> tuple[datetime.datetime, datetime.datetime]:
    return cert.not_valid_before_utc, cert.not_valid_after_utc


class Pki:
    def __init__(self, directory: Path, app_uri: str, names: list[str], client_mode: str = "accept"):
        if client_mode not in ("accept", "trusted"):
            raise ValueError("OPCUA_CLIENT_CERTS must be accept or trusted")
        self.dir = directory
        self.app_uri = app_uri
        self.client_mode = client_mode
        # in Docker the default hostname is the container id, which changes on every
        # recreate; leave it out so the certificate (and clients' trust) survives updates
        hostname = socket.gethostname()
        if re.fullmatch(r"[0-9a-f]{12}", hostname):
            hostname = ""
        self.names = list(dict.fromkeys([n.strip() for n in [hostname, "localhost", *names] if n and n.strip()]))
        self.cert_path = self.dir / "own" / "cert.der"
        self.key_path = self.dir / "own" / "key.pem"
        self._clients_path = self.dir / "clients.json"
        self._clients: dict[str, dict] = {}
        self._load_clients()

    # ---------------------------------------------------------------- own certificate
    def ensure_server_cert(self) -> bool:
        """Create the server certificate if missing, expiring, or with other names. True if new."""
        (self.dir / "own").mkdir(parents=True, exist_ok=True)
        if self.cert_path.exists() and self.key_path.exists():
            cert = x509.load_der_x509_certificate(self.cert_path.read_bytes())
            uris, dns, ips = _san(cert)
            wanted_dns, wanted_ips = self._split_names()
            renew_at = _validity(cert)[1] - datetime.timedelta(days=RENEW_BEFORE_DAYS)
            if (uris == [self.app_uri] and sorted(dns) == sorted(wanted_dns)
                    and sorted(ips) == sorted(wanted_ips)
                    and datetime.datetime.now(datetime.timezone.utc) < renew_at):
                return False
            log.info("Server certificate names or validity changed; generating a new one")
        key = generate_private_key()
        dns, ips = self._split_names()
        san = ([x509.UniformResourceIdentifier(self.app_uri)] + [x509.DNSName(d) for d in dns]
               + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips])
        cert = generate_self_signed_app_certificate(
            key, "M580 OPC UA Simulator", {"organizationName": "M580 OPC UA Simulator"}, san,
            extended=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH], days=CERT_DAYS)
        self.key_path.write_bytes(dump_private_key_as_pem(key))
        self.key_path.chmod(0o600)
        self.cert_path.write_bytes(cert.public_bytes(Encoding.DER))
        log.info("Generated server certificate %s for %s", thumbprint(self.cert_path.read_bytes()), ", ".join(self.names))
        return True

    def _split_names(self) -> tuple[list[str], list[str]]:
        dns, ips = [], []
        for n in self.names:
            try:
                ips.append(str(ipaddress.ip_address(n)))
            except ValueError:
                dns.append(n)
        return dns, ips

    def server_cert(self) -> x509.Certificate:
        return x509.load_der_x509_certificate(self.cert_path.read_bytes())

    def server_cert_info(self) -> dict:
        der = self.cert_path.read_bytes()
        cert = x509.load_der_x509_certificate(der)
        uris, dns, ips = _san(cert)
        nb, na = _validity(cert)
        return {"subject": _cn(cert), "thumbprint": thumbprint(der), "application_uri": uris[0] if uris else "",
                "dns": dns, "ip": ips, "valid_from": nb.timestamp(), "valid_to": na.timestamp(),
                "key_bits": cert.public_key().key_size}

    def server_cert_pem(self) -> bytes:
        return self.server_cert().public_bytes(Encoding.PEM)

    # ---------------------------------------------------------------- client certificates
    def _load_clients(self):
        try:
            self._clients = json.loads(self._clients_path.read_text())
        except (FileNotFoundError, ValueError):
            self._clients = {}

    def _save_clients(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._clients_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._clients, indent=1))
        tmp.replace(self._clients_path)

    def clients(self) -> list[dict]:
        return sorted(self._clients.values(), key=lambda c: -c.get("last_seen", 0))

    def client_der(self, thumb: str) -> bytes:
        if thumb not in self._clients:
            raise KeyError(thumb)
        return (self.dir / "clients" / f"{thumb}.der").read_bytes()

    def set_status(self, thumb: str, status: str):
        if status not in ("trusted", "rejected"):
            raise ValueError("status must be trusted or rejected")
        self._clients[thumb]["status"] = status          # KeyError if unknown
        self._clients[thumb]["decided"] = time.time()
        self._save_clients()

    def remove(self, thumb: str):
        del self._clients[thumb]
        (self.dir / "clients" / f"{thumb}.der").unlink(missing_ok=True)
        self._save_clients()

    async def validate(self, cert: x509.Certificate, app_description: ua.ApplicationDescription):
        """asyncua certificate validator: called at CreateSession when the client sends a certificate."""
        policy_uri, mode = channel_security.get()
        if policy_uri == POLICY_NONE_URI:
            return                                   # no channel security: nothing to authenticate
        der = cert.public_bytes(Encoding.DER)
        thumb = thumbprint(der)
        now = time.time()
        nb, na = _validity(cert)
        uris, _, _ = _san(cert)
        app_uri = app_description.ApplicationUri if app_description else ""
        rec = self._clients.get(thumb)
        if rec is None:
            rec = self._clients[thumb] = {
                "thumbprint": thumb, "subject": _cn(cert), "first_seen": now,
                "status": "auto" if self.client_mode == "accept" else "rejected",
            }
            (self.dir / "clients").mkdir(parents=True, exist_ok=True)
            (self.dir / "clients" / f"{thumb}.der").write_bytes(der)
            log.info("New client certificate %s (%s): %s", thumb, rec["subject"], rec["status"])
        rec.update({
            "application": app_description.ApplicationName.Text if app_description and app_description.ApplicationName else "",
            "application_uri": app_uri, "certificate_uri": uris[0] if uris else "",
            "uri_matches": bool(uris) and app_uri in uris,
            "valid_from": nb.timestamp(), "valid_to": na.timestamp(),
            "last_seen": now, "last_policy": policy_uri.rsplit("#", 1)[-1],
            "last_mode": {2: "Sign", 3: "SignAndEncrypt"}.get(int(mode), str(mode)),
        })
        if not nb.timestamp() <= now <= na.timestamp():
            rec["last_result"] = "BadCertificateTimeInvalid"
            self._save_clients()
            raise ServiceError(ua.StatusCodes.BadCertificateTimeInvalid)
        allowed = rec["status"] == "trusted" or (rec["status"] == "auto" and self.client_mode == "accept")
        rec["last_result"] = "accepted" if allowed else "BadCertificateUntrusted"
        self._save_clients()
        if not allowed:
            log.info("Rejected client certificate %s (%s)", thumb, rec["subject"])
            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

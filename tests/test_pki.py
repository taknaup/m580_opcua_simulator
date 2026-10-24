"""OPC UA certificate and trust tests. Need asyncua (run inside the app image):
    docker run --rm -v "$PWD:/proj:ro" -w /proj m580-opcua-sim:latest python -m unittest discover -s tests
"""
import asyncio
import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from asyncua import ua
    from asyncua.common.utils import ServiceError
    from asyncua.crypto.cert_gen import generate_private_key, generate_self_signed_app_certificate

    from app import pki as pki_mod
    from app.pki import Pki, parse_policies
    HAVE_ASYNCUA = True
except ImportError:
    HAVE_ASYNCUA = False

SECURE_CHANNEL = ("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256", 3)


def client_cert(uri="urn:test:client", expired=False):
    key = generate_private_key()
    if not expired:
        return generate_self_signed_app_certificate(
            key, "Test Client", {}, [x509.UniformResourceIdentifier(uri)],
            extended=[ExtendedKeyUsageOID.CLIENT_AUTH], days=30)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Old Client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=60)).not_valid_after(now - datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False)
            .sign(key, hashes.SHA256()))


def app_desc(uri="urn:test:client", name="Test SCADA"):
    d = ua.ApplicationDescription()
    d.ApplicationUri = uri
    d.ApplicationName = ua.LocalizedText(name)
    return d


@unittest.skipUnless(HAVE_ASYNCUA, "asyncua not installed (run in the app image)")
class PkiTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_validate(self, pki, cert, channel=SECURE_CHANNEL, desc=None):
        async def go():
            pki_mod.channel_security.set(channel)
            await pki.validate(cert, desc or app_desc())
        asyncio.run(go())

    def test_server_certificate_names_and_reuse(self):
        p = Pki(self.dir, "urn:m580-opcua-simulator", ["opcua-sim.localtest.me", "192.168.0.142"])
        self.assertTrue(p.ensure_server_cert())
        info = p.server_cert_info()
        self.assertEqual(info["application_uri"], "urn:m580-opcua-simulator")
        self.assertIn("opcua-sim.localtest.me", info["dns"])
        self.assertIn("localhost", info["dns"])
        self.assertEqual(info["ip"], ["192.168.0.142"])
        self.assertEqual(len(info["thumbprint"]), 40)
        self.assertGreater(info["valid_to"] - info["valid_from"], 4 * 365 * 86400)
        self.assertFalse(p.ensure_server_cert())                    # same names: kept
        p2 = Pki(self.dir, "urn:m580-opcua-simulator", ["other.example"])
        self.assertTrue(p2.ensure_server_cert())                   # names changed: regenerated
        self.assertNotEqual(p2.server_cert_info()["thumbprint"], info["thumbprint"])

    def test_certificate_survives_container_recreate(self):
        from unittest import mock
        with mock.patch("socket.gethostname", return_value="3f2a9c1b7d4e"):     # container id
            a = Pki(self.dir, "urn:x", ["opcua-sim"])
            a.ensure_server_cert()
        with mock.patch("socket.gethostname", return_value="9e8d7c6b5a41"):     # recreated
            b = Pki(self.dir, "urn:x", ["opcua-sim"])
            self.assertFalse(b.ensure_server_cert())
        self.assertNotIn("3f2a9c1b7d4e", a.server_cert_info()["dns"])

    def test_policy_parsing(self):
        self.assertEqual(len(parse_policies("all")), 7)
        self.assertNotIn("None", parse_policies("secure"))
        self.assertEqual(parse_policies("none, basic256sha256_signandencrypt"),
                         ["None", "Basic256Sha256_SignAndEncrypt"])
        with self.assertRaises(ValueError):
            parse_policies("Basic128Rsa15_Sign")

    def test_accept_mode_records_and_accepts(self):
        p = Pki(self.dir, "urn:x", [], "accept")
        cert = client_cert()
        self.run_validate(p, cert)
        [rec] = p.clients()
        self.assertEqual((rec["status"], rec["last_result"], rec["application"]), ("auto", "accepted", "Test SCADA"))
        self.assertTrue(rec["uri_matches"])
        p.set_status(rec["thumbprint"], "rejected")                 # explicit block wins
        with self.assertRaises(ServiceError) as e:
            self.run_validate(p, cert)
        self.assertEqual(e.exception.code, ua.StatusCodes.BadCertificateUntrusted)

    def test_trusted_mode_requires_approval(self):
        p = Pki(self.dir, "urn:x", [], "trusted")
        cert = client_cert()
        with self.assertRaises(ServiceError) as e:
            self.run_validate(p, cert)
        self.assertEqual(e.exception.code, ua.StatusCodes.BadCertificateUntrusted)
        [rec] = p.clients()
        self.assertEqual(rec["status"], "rejected")
        p.set_status(rec["thumbprint"], "trusted")
        self.run_validate(p, cert)                                  # now accepted
        # persisted
        self.assertEqual(Pki(self.dir, "urn:x", [], "trusted").clients()[0]["status"], "trusted")

    def test_unencrypted_channel_is_not_checked(self):
        p = Pki(self.dir, "urn:x", [], "trusted")
        self.run_validate(p, client_cert(), channel=(pki_mod.POLICY_NONE_URI, 1))
        self.assertEqual(p.clients(), [])

    def test_expired_certificate_rejected(self):
        p = Pki(self.dir, "urn:x", [], "accept")
        with self.assertRaises(ServiceError) as e:
            self.run_validate(p, client_cert(expired=True))
        self.assertEqual(e.exception.code, ua.StatusCodes.BadCertificateTimeInvalid)

    def test_ruleset_allows_services_but_not_address_space_changes(self):
        from asyncua.server.users import User, UserRole
        r = pki_mod.SimulatorRuleset()
        user, admin = User(role=UserRole.User), User(role=UserRole.Admin)
        for allowed in (ua.ObjectIds.WriteRequest_Encoding_DefaultBinary, ua.ObjectIds.BrowseNextRequest_Encoding_DefaultBinary,
                        ua.ObjectIds.RegisterNodesRequest_Encoding_DefaultBinary, ua.ObjectIds.ReadRequest_Encoding_DefaultBinary):
            self.assertTrue(r.check_validity(user, ua.NodeId(allowed), None))
        self.assertFalse(r.check_validity(user, ua.NodeId(ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary), None))
        self.assertTrue(r.check_validity(admin, ua.NodeId(ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary), None))

    def test_uri_mismatch_is_reported(self):
        p = Pki(self.dir, "urn:x", [], "accept")
        self.run_validate(p, client_cert(uri="urn:cert"), desc=app_desc(uri="urn:other"))
        self.assertFalse(p.clients()[0]["uri_matches"])


if __name__ == "__main__":
    unittest.main()

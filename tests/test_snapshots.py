"""Snapshot store and PLC write batching.  python3 -m unittest discover -s tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plc_source import plan_writes  # noqa: E402
from app.snapshots import SnapshotError, SnapshotStore  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore(Path(tempfile.mkdtemp()) / "snapshots")

    def test_save_list_load_delete(self):
        a = self.store.save("CDU2 pump fault", {"A.B": 1, "C": True}, "P", "export")
        self.assertEqual(a["count"], 2)
        self.assertEqual([s["name"] for s in self.store.list()], ["CDU2 pump fault"])
        self.assertNotIn("values", self.store.list()[0])          # listing stays small
        self.assertEqual(self.store.load(a["id"])["values"], {"A.B": 1, "C": True})
        self.store.delete(a["id"])
        self.assertEqual(self.store.list(), [])

    def test_same_name_gets_distinct_ids(self):
        a = self.store.save("x", {}, "P", "plc")
        b = self.store.save("x", {}, "P", "plc")
        self.assertNotEqual(a["id"], b["id"])

    def test_rejects_bad_names_and_ids(self):
        with self.assertRaises(SnapshotError):
            self.store.save("  ", {}, "P", "plc")
        for bad in ("../../etc/passwd", "x", "20261024102751-a/b"):
            with self.assertRaises(SnapshotError):
                self.store.load(bad)

    def test_import(self):
        doc = self.store.import_doc(json.dumps({"name": "From file", "values": {"T": 5}}).encode(), "P", "export")
        self.assertEqual((doc["name"], doc["count"], doc["note"]), ("From file", 1, "imported"))
        for bad in (b"not json", b"[]", json.dumps({"values": [1, 2]}).encode()):
            with self.assertRaises(SnapshotError):
                self.store.import_doc(bad, "P", "export")


class PlanWritesTests(unittest.TestCase):
    def test_small_writes_share_a_request(self):
        items = [(46, i * 10, b"\x00" * 4) for i in range(60)]
        batches = plan_writes(items, 975)
        self.assertEqual(len(batches), 1)                         # 5 + 60 x (9 + 4) = 785 bytes

    def test_request_size_limit(self):
        items = [(46, i * 10, b"\x00" * 4) for i in range(200)]
        batches = plan_writes(items, 975)
        self.assertEqual([len(b) for b in batches], [74, 74, 52])  # (975 - 5) // 13 = 74
        self.assertEqual(sum(len(b) for b in batches), 200)

    def test_large_value_gets_its_own_request(self):
        batches = plan_writes([(46, 0, b"\x00" * 900), (46, 1000, b"\x00" * 100)], 975)
        self.assertEqual([len(b) for b in batches], [1, 1])


if __name__ == "__main__":
    unittest.main()

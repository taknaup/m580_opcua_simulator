"""PLC dictionary -> tag layout tests (no PLC needed).  python3 -m unittest discover -s tests"""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plc_source import Layout  # noqa: E402
from app.umas import MemberRecord, TypeRecord, VarRecord  # noqa: E402

# type ids as a Control Expert PLC reports them (elementary ids are fixed)
INNER, OUTER, STR32, ARR, GRID, ARR_DDT = 30, 31, 62, 70, 71, 72
TYPES = [
    TypeRecord(INNER, "inner_t", 2, 6),
    TypeRecord(OUTER, "outer_t", 2, 80),
    TypeRecord(STR32, "string[32]", 1, 32),
    TypeRecord(ARR, "ARRAY[0..2] OF INT", 4, 6),
    TypeRecord(GRID, "ARRAY[0..1,1..2] OF BOOL", 4, 4),
    TypeRecord(ARR_DDT, "ARRAY[1..2] OF inner_t", 4, 12),
]
MEMBERS = {
    INNER: [MemberRecord("Cmd", 1, 0), MemberRecord("Sp", 8, 2)],
    OUTER: [MemberRecord("In", INNER, 0), MemberRecord("Arr", ARR, 6), MemberRecord("Grid", GRID, 12),
            MemberRecord("Name", STR32, 16), MemberRecord("Many", ARR_DDT, 48), MemberRecord("T", 10, 60)],
}
VARS = [VarRecord("Dev1", OUTER, 46, 1000), VarRecord("Plain", 6, 46, 4000), VarRecord("Bit", 25, 46, 4004),
        VarRecord("Odd", 99, 46, 5000)]


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Layout("TEST", TYPES, VARS, MEMBERS)
        cls.leaf = cls.L.leaves

    def test_paths_match_export_naming(self):
        self.assertEqual(set(self.leaf), {
            "Dev1.In.Cmd", "Dev1.In.Sp", "Dev1.Arr[0]", "Dev1.Arr[1]", "Dev1.Arr[2]",
            "Dev1.Grid[0,1]", "Dev1.Grid[0,2]", "Dev1.Grid[1,1]", "Dev1.Grid[1,2]", "Dev1.Name",
            "Dev1.Many[1].Cmd", "Dev1.Many[1].Sp", "Dev1.Many[2].Cmd", "Dev1.Many[2].Sp", "Dev1.T",
            "Plain", "Bit"})

    def test_nested_offsets_are_absolute(self):
        # plc4py placed nested members relative to their DDT; they must include every parent offset
        self.assertEqual(self.leaf["Dev1.In.Sp"].offset, 1002)
        self.assertEqual(self.leaf["Dev1.Arr[2]"].offset, 1010)
        self.assertEqual(self.leaf["Dev1.Grid[1,2]"].offset, 1015)
        self.assertEqual(self.leaf["Dev1.Many[2].Sp"].offset, 1000 + 48 + 6 + 2)

    def test_types(self):
        t = {x.path: x.type_name for x in self.L.tags}
        self.assertEqual(t["Dev1.Name"], "STRING[32]")
        self.assertEqual(t["Bit"], "EBOOL")
        self.assertEqual(t["Dev1.T"], "TIME")
        self.assertTrue(all(x.writable for x in self.L.tags))

    def test_unknown_type_warns(self):
        self.assertTrue(any("Odd" in w for w in self.L.warnings))

    def test_ranges_cover_all_leaves(self):
        for leaf in self.leaf.values():
            self.assertTrue(any(b == leaf.block and s <= leaf.offset and leaf.offset + leaf.size <= s + n
                                for b, s, n in self.L.ranges))
        self.assertLessEqual(len(self.L.ranges), 2)

    def test_decode_encode_roundtrip(self):
        L = self.L
        cases = [("Dev1.In.Sp", 43.5), ("Dev1.Arr[1]", -7), ("Plain", 123456), ("Dev1.Name", "abc"),
                 ("Dev1.In.Cmd", True), ("Dev1.T", 30000)]
        for path, v in cases:
            leaf = L.leaves[path]
            raw = L.encode(leaf, v)
            self.assertEqual(len(raw), leaf.size, path)
            self.assertEqual(L.decode(leaf, raw, 0), v, path)

    def test_ebool_keeps_status_bits(self):
        leaf = self.L.leaves["Bit"]
        self.assertEqual(self.L.encode(leaf, True, current_byte=0b0000_0100), bytes([0b0000_0101]))
        self.assertIs(self.L.decode(leaf, bytes([0b0000_0110]), 0), False)

    def test_decode_little_endian(self):
        self.assertEqual(self.L.decode(self.L.leaves["Plain"], struct.pack("<i", 30000), 0), 30000)


if __name__ == "__main__":
    unittest.main()

"""Parser tests. Run from the project root:  python3 -m unittest discover -s tests"""
import io
import sys
import unittest
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.iec_values import coerce, parse_literal, type_info  # noqa: E402
from app.xef_parser import ParseError, parse_program  # noqa: E402

REFERENCE = ROOT / "HAC_SKID_v00_01_003.ZEF"

SAMPLE = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ZEFExchangeFile>
  <fileHeader company="Schneider Automation" product="Control Expert V16.2" DTDVersion="41"></fileHeader>
  <contentHeader name="TEST_APP" version="1.2.3"><comment>Password: secret</comment></contentHeader>
  <IOConf><PLC><partItem family="M580" partNumber="BMEP582040"></partItem></PLC></IOConf>
  <DDTSource DDTName="inner_t">
    <structure>
      <variables name="Cmd" typeName="BOOL"><comment>R/O - Command</comment></variables>
      <variables name="Sp" typeName="REAL"><comment>R/W - Setpoint</comment><variableInit value="12.5"></variableInit></variables>
    </structure>
  </DDTSource>
  <DDTSource DDTName="outer_t">
    <structure>
      <variables name="In" typeName="inner_t">
        <comment>Inner block</comment>
        <instanceElementDesc name="Cmd"><comment>R/W - Command (overridden)</comment></instanceElementDesc>
      </variables>
      <variables name="Arr" typeName="ARRAY[0..2] OF INT"><comment>R/W - Array</comment></variables>
      <variables name="Grid" typeName="ARRAY[0..1,1..2] OF BOOL"><comment>R/O - Grid</comment></variables>
      <variables name="Name" typeName="string[8]"><comment>R/W - Name</comment><variableInit value="'abc'"></variableInit></variables>
      <variables name="Tmr" typeName="TIME"><comment>R/W - Timer</comment><variableInit value="t#1m30s"></variableInit></variables>
    </structure>
  </DDTSource>
  <dataBlock>
    <variables name="Dev1" typeName="outer_t">
      <comment>Device one</comment>
      <attribute name="IsVariableHMI" value="-1"></attribute>
      <instanceElementDesc name="In">
        <instanceElementDesc name="Sp"><value>99.0</value></instanceElementDesc>
      </instanceElementDesc>
      <instanceElementDesc name="Arr">
        <instanceElementDesc name="[1]"><value>16#10</value></instanceElementDesc>
      </instanceElementDesc>
    </variables>
    <variables name="Plain" typeName="DINT">
      <comment>No prefix here</comment>
      <attribute name="IsVariableHMI" value="-1"></attribute>
    </variables>
    <variables name="Located" typeName="EBOOL" topologicalAddress="%M10">
      <comment>R/O - located bit</comment>
      <attribute name="IsVariableHMI" value="-1"></attribute>
    </variables>
    <variables name="Hidden" typeName="INT"><comment>R/W - not HMI</comment></variables>
    <variables name="NotHmi" typeName="INT">
      <comment>R/W - flag 0</comment>
      <attribute name="IsVariableHMI" value="0"></attribute>
    </variables>
    <variables name="Odd" typeName="SOME_IODDT">
      <attribute name="IsVariableHMI" value="-1"></attribute>
    </variables>
  </dataBlock>
</ZEFExchangeFile>
"""


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = parse_program(SAMPLE, "sample.xef")
        cls.t = {t.path: t for t in cls.p.tags}

    def test_header(self):
        self.assertEqual((self.p.name, self.p.version, self.p.plc), ("TEST_APP", "1.2.3", "BMEP582040"))
        self.assertEqual(self.p.hmi_variables, 4)

    def test_only_hmi_variables(self):
        self.assertNotIn("Hidden", self.t)
        self.assertNotIn("NotHmi", self.t)

    def test_expansion_paths(self):
        expected = {"Dev1.In.Cmd", "Dev1.In.Sp", "Dev1.Arr[0]", "Dev1.Arr[1]", "Dev1.Arr[2]",
                    "Dev1.Grid[0,1]", "Dev1.Grid[0,2]", "Dev1.Grid[1,1]", "Dev1.Grid[1,2]",
                    "Dev1.Name", "Dev1.Tmr", "Plain", "Located"}
        self.assertEqual(set(self.t), expected)

    def test_comment_override_changes_access(self):
        cmd = self.t["Dev1.In.Cmd"]
        self.assertEqual(cmd.comment, "R/W - Command (overridden)")
        self.assertTrue(cmd.writable)
        self.assertEqual(cmd.context, ["Device one", "Inner block"])

    def test_access_from_prefix(self):
        self.assertTrue(self.t["Dev1.In.Sp"].writable)
        self.assertFalse(self.t["Dev1.Grid[1,2]"].writable)
        self.assertEqual(self.t["Dev1.Grid[1,2]"].comment, "R/O - Grid")

    def test_default_access(self):
        self.assertEqual(self.t["Plain"].access, "default")
        self.assertTrue(self.t["Plain"].writable)
        ro = {t.path: t for t in parse_program(SAMPLE, "s.xef", default_writable=False).tags}
        self.assertFalse(ro["Plain"].writable)

    def test_initial_values(self):
        self.assertEqual(self.t["Dev1.In.Sp"].initial, 99.0)       # instance overrides DDT default 12.5
        self.assertEqual(self.t["Dev1.Arr[1]"].initial, 16)
        self.assertEqual(self.t["Dev1.Arr[0]"].initial, 0)
        self.assertEqual(self.t["Dev1.Name"].initial, "abc")
        self.assertEqual(self.t["Dev1.Tmr"].initial, 90_000)

    def test_located_variable(self):
        self.assertEqual(self.t["Located"].address, "%M10")
        self.assertEqual(self.t["Located"].type_name, "EBOOL")

    def test_unknown_type_warns(self):
        self.assertTrue(any("SOME_IODDT" in w for w in self.p.warnings))

    def test_header_comment_not_exposed(self):
        self.assertFalse(any("secret" in t.comment for t in self.p.tags))

    def test_zef_container(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("props.xml", "<Properties/>")
            z.writestr("unitpro.xef", SAMPLE)
        self.assertEqual(len(parse_program(buf.getvalue(), "x.zef").tags), len(self.p.tags))

    def test_bad_input(self):
        with self.assertRaises(ParseError):
            parse_program(b"not xml", "x.xef")
        with self.assertRaises(ParseError):
            parse_program(b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a/>', "x.xef")


class ValueTests(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(parse_literal("2#0000_0101", type_info("WORD")), 5)
        self.assertEqual(parse_literal("INT#-7", type_info("INT")), -7)
        self.assertEqual(parse_literal("T#1h2m3s4ms", type_info("TIME")), 3_723_004)
        self.assertIs(parse_literal("TRUE", type_info("EBOOL")), True)
        self.assertEqual(parse_literal("'a$'b'", type_info("STRING")), "a'b")

    def test_coerce_ranges(self):
        with self.assertRaises(ValueError):
            coerce(40000, type_info("INT"))
        with self.assertRaises(ValueError):
            coerce(-1, type_info("UINT"))
        with self.assertRaises(ValueError):
            coerce("1.5", type_info("DINT"))
        with self.assertRaises(ValueError):
            coerce("123456789", type_info("STRING[8]"))
        with self.assertRaises(ValueError):
            coerce("nan", type_info("REAL"))
        self.assertEqual(coerce("16#FF", type_info("INT")), 255)
        self.assertIs(coerce("false", type_info("BOOL")), False)


@unittest.skipUnless(REFERENCE.exists(), "reference program not present")
class ReferenceProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = parse_program(REFERENCE.read_bytes(), REFERENCE.name)
        cls.t = {t.path: t for t in cls.p.tags}

    def test_counts(self):
        self.assertEqual(self.p.hmi_variables, 1806)
        self.assertEqual(len(self.p.tags), 6050)
        self.assertEqual(Counter(t.access for t in self.p.tags), {"R/O": 3320, "R/W": 1738, "default": 992})
        self.assertEqual(self.p.warnings, [])

    def test_breaker(self):
        self.assertTrue(self.t["HAC_RPP2_BRKR07.CntrlSel.ManCmd"].writable)
        self.assertFalse(self.t["HAC_RPP2_BRKR07.OpnCmd"].writable)
        self.assertEqual(self.t["HAC_RPP2_BRKR07.StsAlrt.Dly"].initial, 30000)
        self.assertIs(self.t["HAC_RPP2_BRKR07.CntrlSel.ManAuto"].initial, True)


if __name__ == "__main__":
    unittest.main()

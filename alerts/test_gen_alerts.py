"""Run with:  python3 -m unittest alerts.test_gen_alerts   (from the repo root)"""
import copy
import unittest
from pathlib import Path

import yaml

from alerts import gen_alerts as g

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "alerts" / "alerts.yml").read_text())


def read(name):
    return (ROOT / "radio" / "MODELS" / name).read_bytes().decode("utf-8")


class GenAlerts(unittest.TestCase):
    def run_gen(self, name, cfg=CFG):
        notes = []
        out, kept_ls, kept_cf, ported = g.generate(read(name), cfg, cfg["models"][name], notes)
        g.verify(read(name), out, kept_ls, kept_cf, ported)
        return out, notes

    def test_only_alert_sections_change_and_crlf_kept(self):
        for name in CFG["models"]:
            out, _ = self.run_gen(name)
            self.assertNotIn("\n", out.replace("\r\n", ""))
            self.assertTrue(out.endswith("\r\n"))

    def test_expected_values(self):
        out, _ = self.run_gen("model02.yml")
        data = yaml.safe_load(out)
        self.assertEqual(data["logicalSw"][0]["def"], "tele(10),70")
        self.assertEqual(data["logicalSw"][0]["andsw"], "!L2")
        self.assertEqual(data["logicalSw"][0]["delay"], 50)
        self.assertEqual(data["logicalSw"][1]["def"], "tele(10),66")
        self.assertEqual(data["customFn"][5]["def"], "Wrn1,1,4")

    def test_gvar_adjusters_kept_on_m07r(self):
        out, _ = self.run_gen("model01.yml")
        before = yaml.safe_load(read("model01.yml"))["customFn"]
        after = yaml.safe_load(out)["customFn"]
        for slot in (0, 1, 2):
            self.assertEqual(before[slot], after[slot])

    def test_gvar_mix_ported_to_bd8(self):
        out, notes = self.run_gen("model02.yml")
        a, b = yaml.safe_load(read("model01.yml")), yaml.safe_load(out)
        self.assertEqual(a["gvars"], b["gvars"])
        for slot in (0, 1, 2):
            self.assertEqual(a["customFn"][slot], b["customFn"][slot])
        self.assertEqual(b["expoData"][:2], a["expoData"][:2])
        self.assertEqual(b["expoData"][2]["curve"]["value"], 0)
        fm = b["flightModeData"][0]
        self.assertEqual([t["value"] for t in fm["trim"].values()], [0, 0])
        self.assertEqual([fm["gvars"][i]["val"] for i in range(4)], [100, 100, 0, 100])
        self.assertEqual((b["thrTrim"], b["displayTrims"], b["trimInc"]), (1, 2, -1))

    def test_second_pass_is_stable(self):
        out, _ = self.run_gen("model02.yml")
        notes = []
        again, *_ = g.generate(out, CFG, CFG["models"]["model02.yml"], notes)
        self.assertEqual(out, again)
        self.assertIn("gvar mixing already present, left alone", notes)

    def test_practice_profile(self):
        cfg = copy.deepcopy(CFG)
        cfg["active_profile"] = "practice"
        out, _ = self.run_gen("model02.yml", cfg)
        self.assertEqual(yaml.safe_load(out)["logicalSw"][0]["def"], "tele(10),72")

    def test_refuses_to_overwrite_without_flag(self):
        cfg = copy.deepcopy(CFG)
        cfg["models"]["model01.yml"]["replace_existing"] = False
        with self.assertRaises(g.GenError):
            self.run_gen("model01.yml", cfg)

    def test_rejects_unknown_sound(self):
        cfg = copy.deepcopy(CFG)
        cfg["actions"]["alarm"][1]["name"] = "Nope"
        with self.assertRaises(g.GenError):
            self.run_gen("model02.yml", cfg)


if __name__ == "__main__":
    unittest.main()

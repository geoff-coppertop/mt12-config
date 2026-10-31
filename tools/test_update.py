"""Run with:  uv run python -m unittest tools.test_update   (from the repo root)"""
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools import update as u


def make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"

    def tearDown(self):
        self.tmp.cleanup()


class FetchTests(Base):
    def test_verifies_and_caches(self):
        data = b"payload"
        calls = []
        get = lambda url: calls.append(url) or data  # noqa: E731
        entry = {"url": "https://x/y.zip", "sha256": sha(data)}
        path = u.fetch(entry, self.cache, get)
        self.assertEqual(path.read_bytes(), data)
        u.fetch(entry, self.cache, get)
        self.assertEqual(len(calls), 1)  # second call served from cache

    def test_rejects_mismatch_and_writes_nothing(self):
        with self.assertRaises(u.UpdateError):
            u.fetch({"url": "https://x", "sha256": sha(b"good")}, self.cache, lambda url: b"evil")
        self.assertFalse(self.cache.exists())

    def test_refuses_unpinned(self):
        with self.assertRaises(u.UpdateError):
            u.fetch({"url": "https://x", "sha256": None}, self.cache, lambda url: b"")


class MergeTests(Base):
    def test_leaves_settings_and_widgets_alone(self):
        card = self.root / "card"
        (card / "MODELS").mkdir(parents=True)
        (card / "MODELS" / "model01.yml").write_bytes(b"mine\r\n")
        (card / "SCRIPTS").mkdir()
        (card / "SCRIPTS" / "same.lua").write_bytes(b"x")
        archive = self.root / "pack.zip"
        archive.write_bytes(make_zip({
            "MODELS/README.txt": b"pack", "MODELS/model01.yml": b"pack", "RADIO/radio.yml": b"pack",
            "WIDGETS/w/main.lua": b"colour only", "SCRIPTS/same.lua": b"x",
            "SCRIPTS/TOOLS/new.lua": b"new\r\n", "edgetx.sdcard.version": b"2.12.3\n",
        }))
        self.assertEqual(u.merge_zip(archive, card), (2, 1))
        self.assertEqual((card / "MODELS" / "model01.yml").read_bytes(), b"mine\r\n")
        self.assertFalse((card / "RADIO").exists() or (card / "WIDGETS").exists())
        self.assertEqual((card / "SCRIPTS" / "TOOLS" / "new.lua").read_bytes(), b"new\r\n")
        self.assertEqual((card / "edgetx.sdcard.version").read_bytes(), b"2.12.3\n")

    def test_dry_run_writes_nothing(self):
        card = self.root / "card"
        card.mkdir()
        archive = self.root / "pack.zip"
        archive.write_bytes(make_zip({"SOUNDS/en/a.wav": b"w"}))
        self.assertEqual(u.merge_zip(archive, card, dry_run=True), (1, 0))
        self.assertFalse((card / "SOUNDS").exists())

    def test_rejects_path_traversal(self):
        archive = self.root / "pack.zip"
        archive.write_bytes(make_zip({"../escape.txt": b"x"}))
        with self.assertRaises(u.UpdateError):
            u.merge_zip(archive, self.root / "card")


class FirmwareTests(Base):
    def test_picks_the_mt12_image(self):
        archive = self.root / "fw.zip"
        archive.write_bytes(make_zip({"fw.json": b"{}", "mt12-0ebe2e9.bin": b"\x00" * 10, "tx16s-0ebe2e9.bin": b"\x01"}))
        self.assertEqual(u.firmware_image(archive), b"\x00" * 10)

    def test_rejects_missing_or_oversize(self):
        archive = self.root / "fw.zip"
        archive.write_bytes(make_zip({"tx16s-0ebe2e9.bin": b"\x01"}))
        with self.assertRaises(u.UpdateError):
            u.firmware_image(archive)
        archive.write_bytes(make_zip({"mt12-0ebe2e9.bin": b"\x00" * (u.FLASH_SIZE + 1)}))
        with self.assertRaises(u.UpdateError):
            u.firmware_image(archive)

    def test_dfu_backs_up_whole_flash_then_writes_at_base(self):
        read_back, write = u.dfu_commands(Path("new.bin"), Path("old.bin"))
        self.assertEqual(read_back, ["dfu-util", "-d", "0483:df11", "-a", "0", "-s", "0x08000000:1048576", "-U", "old.bin"])
        self.assertEqual(write, ["dfu-util", "-d", "0483:df11", "-a", "0", "-s", "0x08000000:leave", "-D", "new.bin"])

    def test_flash_waits_for_dfu_then_runs_both_steps(self):
        listings = iter(["nothing", "Found DFU: [0483:df11] ver=2200"])
        ran = []

        def runner(cmd):
            if cmd == ["dfu-util", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, next(listings), "")
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        prompts = []
        with mock.patch.object(u, "FLASH_BACKUPS", self.root / "fb"), mock.patch("shutil.which", return_value="/bin/dfu-util"):
            u.flash_firmware(b"img", prompts.append, dry_run=False, runner=runner, stamp="t")
        self.assertEqual(len(prompts), 2)  # plug in, then the retry
        self.assertEqual([c[-2] for c in ran], ["-U", "-D"])
        self.assertEqual((self.root / "fb" / "t-new.bin").read_bytes(), b"img")

    def test_flash_failure_raises(self):
        def runner(cmd):
            out = "Found DFU: [0483:df11]" if cmd[-1] == "-l" else ""
            return subprocess.CompletedProcess(cmd, 1 if "-D" in cmd else 0, out, "boom")
        with mock.patch.object(u, "FLASH_BACKUPS", self.root / "fb"), mock.patch("shutil.which", return_value="x"):
            with self.assertRaises(u.UpdateError):
                u.flash_firmware(b"img", lambda p: "", dry_run=False, runner=runner, stamp="t")


class ElrsTests(Base):
    def test_module_command(self):
        cmd = u.elrs_command(Path("/f"), "radiomaster.tx_2400.mt12", "etx", "/dev/ttyACM0", True, False, None)
        self.assertEqual(cmd[1:], ["/f/flasher.pyz", "--dir", "/f", "--target", "radiomaster.tx_2400.mt12",
                                   "--flash", "etx", "--tx", "--port", "/dev/ttyACM0"])

    def test_receiver_command_with_lbt_and_phrase(self):
        cmd = u.elrs_command(Path("/f"), "radiomaster.rx_2400.er3", "wifi", "10.0.0.1", False, True, "secret")
        self.assertNotIn("--tx", cmd)
        self.assertEqual(cmd[-5:], ["--lbt", "--port", "10.0.0.1", "--phrase", "secret"])

    def test_phrase_is_not_printed(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            u.run_flasher(["py", "--phrase", "secret"], dry_run=True)
        self.assertNotIn("secret", out.getvalue())


def fake_api(elrs_tags=None):
    def asset(name, digest=None):
        return {"name": name, "browser_download_url": f"https://dl/{name}", "digest": digest}
    pages = {
        "EdgeTX/edgetx/releases/latest": {"tag_name": "v2.12.6", "assets": [
            asset("edgetx-firmware-v2.12.6.zip", "sha256:" + "a" * 64), asset("edgetx-cpn-linux-x64.AppImage")]},
        "EdgeTX/edgetx-sdcard/releases": [
            {"tag_name": "v2.13.0-rc1", "prerelease": True, "assets": [asset("bw128x64.zip")]},
            {"tag_name": "v2.12.3", "assets": [asset("bw128x64.zip", "sha256:" + "b" * 64)]}],
        "EdgeTX/edgetx-sdcard-sounds/releases": [
            {"tag_name": "v2.12.3", "assets": [asset("edgetx-sdcard-sounds-en-2.12.3.zip", "sha256:" + "c" * 64)]}],
        "ExpressLRS/Lua/releases/latest": {"tag_name": "v1.0.0", "assets": [
            asset("expresslrs-lua-legacy.zip"), asset("expresslrs-lua-v1.0.0.zip", "sha256:" + "d" * 64)]},
    }

    def get(url):
        if url == u.ELRS_INDEX:
            return json.dumps({"tags": elrs_tags or {"4.1.0": "abc", "4.2.0-RC1": "rc", "4.1.1": "def"}}).encode()
        if url.startswith("https://api.github.com/repos/"):
            return json.dumps(pages[url.removeprefix("https://api.github.com/repos/")]).encode()
        return b"downloaded " + url.encode()
    return get


class CheckTests(Base):
    def pins_file(self):
        path = self.root / "versions.yml"
        path.write_text((Path(u.ROOT) / "versions.yml").read_text())
        return path

    def test_latest_skips_prereleases_and_rcs(self):
        new = u.latest(u.load_pins(self.pins_file()), fake_api())
        self.assertEqual(new["sdcard"]["tag"], "v2.12.3")
        self.assertEqual(new["elrs"], {"tag": "4.1.1", "commit": "def", "sha256": None,
                                       "url": "https://artifactory.expresslrs.org/ExpressLRS/def/firmware.zip"})
        self.assertEqual(new["edgetx"]["sha256"], "a" * 64)

    def test_check_without_bump_changes_nothing(self):
        path = self.pins_file()
        before = path.read_text()
        with mock.patch("sys.stdout", io.StringIO()):
            u.cmd_check(False, fake_api(), path)
        self.assertEqual(path.read_text(), before)

    def test_bump_moves_pins_keeps_header_and_settings(self):
        path = self.pins_file()
        with mock.patch("sys.stdout", io.StringIO()):
            u.cmd_check(True, fake_api(), path)
        pins = u.load_pins(path)
        self.assertTrue(path.read_text().startswith("# What `card.py update` installs."))
        self.assertEqual(pins["edgetx"]["tag"], "v2.12.6")
        self.assertEqual(pins["elrs"]["sha256"], sha(b"downloaded https://artifactory.expresslrs.org/ExpressLRS/def/firmware.zip"))
        self.assertEqual(pins["elrs"]["module"]["target"], "radiomaster.tx_2400.mt12")
        self.assertEqual(pins["sounds"]["language"], "en")
        self.assertEqual(pins["sdcard"]["tag"], "v2.12.3")  # unchanged
        self.assertNotIn("{", path.read_text())  # block style, so a bump diffs only the changed lines

    def test_major_elrs_change_warns(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            u.cmd_check(False, fake_api({"5.0.0": "x"}), self.pins_file())
        self.assertIn("module and receivers must all be flashed together", out.getvalue())


class UpdateFlowTests(Base):
    def test_card_steps_back_up_first_and_skip_firmware_and_elrs(self):
        card = self.root / "card"
        (card / "MODELS").mkdir(parents=True)
        packs = {}
        for key, entries in (("sdcard", {"SCRIPTS/a.lua": b"a"}), ("sounds", {"SOUNDS/en/b.wav": b"b"}),
                             ("elrs_lua", {"SCRIPTS/ELRS/c.lua": b"c", "WIDGETS/x.lua": b"w"})):
            packs[key] = self.root / f"{key}.zip"
            packs[key].write_bytes(make_zip(entries))
        order = []
        pins = {k: {"tag": "t"} for k in ("edgetx", "sdcard", "sounds", "elrs_lua", "elrs")}
        fetch = lambda entry: packs[next(k for k, v in pins.items() if v is entry)]  # noqa: E731
        with mock.patch.object(u, "load_pins", return_value=pins), mock.patch.object(u, "fetch", side_effect=fetch), \
                mock.patch("tools.card.find_card", return_value=card), \
                mock.patch("tools.card.cmd_pull", side_effect=lambda *a, **k: order.append(k["message"]) or 0), \
                mock.patch("sys.stdout", io.StringIO()):
            u.cmd_update(["sdcard", "sounds", "lua"], None, dry_run=False, git_push=False, ask=lambda p: "")
        self.assertEqual(order, ["Backup before update"])
        self.assertEqual((card / "SCRIPTS" / "ELRS" / "c.lua").read_bytes(), b"c")
        self.assertTrue((card / "SOUNDS" / "en" / "b.wav").exists())
        self.assertFalse((card / "WIDGETS").exists())


if __name__ == "__main__":
    unittest.main()

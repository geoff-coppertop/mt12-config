"""Run with:  uv run python -m unittest tools.test_card   (from the repo root)"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools import card as c

RADIO_YML = b"checksum: 1\r\nmanuallyEdited: 0\r\ntimezoneMinutes: 0\r\nppmunit: 0\r\nsemver: 2.11.3\r\nboard: mt12\r\n"


def write(path: Path, data: bytes, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))


def make_tree(base: Path, m1=b"a: 1\r\n", m2=b"b: 2\r\n"):
    write(base / "RADIO" / "radio.yml", RADIO_YML)
    write(base / "MODELS" / "model01.yml", m1)
    write(base / "MODELS" / "model02.yml", m2)
    write(base / "edgetx.sdcard.version", b"2.8")


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.card, self.radio, self.build = self.root / "card", self.root / "repo" / "radio", self.root / "repo" / "build" / "MODELS"
        make_tree(self.card)
        make_tree(self.radio)

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_card_explicit_and_rejects_other_folders(self):
        self.assertEqual(c.find_card(str(self.card)), self.card)
        with self.assertRaises(c.CardError):
            c.find_card(str(self.root))  # no RADIO/radio.yml

    def test_rejects_other_board(self):
        write(self.card / "RADIO" / "radio.yml", RADIO_YML.replace(b"mt12", b"tx16s"))
        with self.assertRaises(c.CardError):
            c.find_card(str(self.card))

    def test_pull_copies_bytes_exactly(self):
        write(self.card / "MODELS" / "model01.yml", b"a: 99\r\nz: 0\r\n")
        c.cmd_pull(self.card, self.radio, use_git=False, push=False, root=self.root)
        self.assertEqual((self.radio / "MODELS" / "model01.yml").read_bytes(), b"a: 99\r\nz: 0\r\n")

    def test_push_refuses_when_card_has_unbacked_changes(self):
        write(self.card / "MODELS" / "model01.yml", b"changed at the track\r\n")
        write(self.build / "model01.yml", b"generated\r\n", mtime=time.time() + 5)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 2)
        self.assertEqual((self.card / "MODELS" / "model01.yml").read_bytes(), b"changed at the track\r\n")

    def test_push_refuses_stale_build(self):
        write(self.build / "model01.yml", b"generated\r\n", mtime=time.time() - 3600)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 2)

    def test_push_writes_saves_previous_and_keeps_crlf(self):
        write(self.build / "model01.yml", b"generated\r\nx: 1\r\n", mtime=time.time() + 5)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 0)
        self.assertEqual((self.card / "MODELS" / "model01.yml").read_bytes(), b"generated\r\nx: 1\r\n")
        saved = list((self.build.parent / "previous").glob("*/MODELS/model01.yml"))
        self.assertEqual([p.read_bytes() for p in saved], [b"a: 1\r\n"])

    def test_push_twice_without_pull_is_allowed(self):
        write(self.build / "model01.yml", b"v1\r\n", mtime=time.time() + 5)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 0)
        write(self.build / "model01.yml", b"v2\r\n", mtime=time.time() + 6)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 0)
        self.assertEqual((self.card / "MODELS" / "model01.yml").read_bytes(), b"v2\r\n")
        write(self.card / "MODELS" / "model01.yml", b"tweaked at the track\r\n")  # not what we pushed
        write(self.build / "model01.yml", b"v3\r\n", mtime=time.time() + 7)
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=False, dry_run=False), 2)

    def test_force_overrides_and_dry_run_writes_nothing(self):
        write(self.card / "MODELS" / "model01.yml", b"changed\r\n")
        write(self.build / "model01.yml", b"generated\r\n", mtime=time.time() + 5)
        c.cmd_push(self.card, self.radio, self.build, force=True, dry_run=True)
        self.assertEqual((self.card / "MODELS" / "model01.yml").read_bytes(), b"changed\r\n")
        self.assertEqual(c.cmd_push(self.card, self.radio, self.build, force=True, dry_run=False), 0)
        self.assertEqual((self.card / "MODELS" / "model01.yml").read_bytes(), b"generated\r\n")

    def test_pull_commits_only_when_changed(self):
        run = lambda *a: subprocess.run(["git", "-C", str(self.root / "repo"), *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        run("config", "core.autocrlf", "true")  # what Windows installs default to; must not touch yml
        (self.root / "repo" / ".gitattributes").write_text("*.yml -text\n*.version -text\n")
        run("add", "-A")
        run("commit", "-q", "-m", "init")
        self.assertEqual(c.cmd_pull(self.card, self.radio, use_git=True, push=False, root=self.root / "repo"), 0)
        self.assertEqual(run("log", "--oneline").stdout.count(b"\n"), 1)  # no new commit
        write(self.card / "MODELS" / "model02.yml", b"b: 3\r\n")
        c.cmd_pull(self.card, self.radio, use_git=True, push=False, root=self.root / "repo")
        self.assertEqual(run("log", "--oneline").stdout.count(b"\n"), 2)
        blob = run("show", "HEAD:radio/MODELS/model02.yml").stdout
        self.assertEqual(blob, b"b: 3\r\n")  # CRLF survived autocrlf


if __name__ == "__main__":
    unittest.main()

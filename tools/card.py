#!/usr/bin/env python3
"""Sync the MT12 SD card with this repo: back up from the card, push generated models to it.

  uv run tools/card.py status            what differs between card and radio/
  uv run tools/card.py pull              card -> radio/, then commit and push to GitHub
  uv run tools/card.py push              build/MODELS -> card (refuses if the card has unbacked changes)

The card is found automatically (Windows drive letters, WSL /mnt/<letter>, Linux
/media and /run/media). Override with --card PATH or the MT12_CARD variable.
Files are copied as bytes, so the radio's CRLF line endings survive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import string
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARD = "mt12"


class CardError(Exception):
    pass


# ------------------------------------------------------------------ discovery

def is_card(path: Path) -> bool:
    """True if `path` looks like an MT12 SD card root."""
    radio = path / "RADIO" / "radio.yml"
    try:
        if not (radio.is_file() and (path / "MODELS").is_dir()):
            return False
        head = radio.read_bytes()[:400].decode("utf-8", "replace")
    except OSError:
        return False
    return f"board: {BOARD}" in head


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def candidate_roots() -> list[Path]:
    if os.name == "nt":
        return [Path(f"{letter}:/") for letter in string.ascii_uppercase]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    roots = [Path(f"/mnt/{letter}") for letter in string.ascii_lowercase]
    for base in (Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
        if base.is_dir():
            try:
                roots += [p for p in base.iterdir() if p.is_dir()]
            except OSError:
                pass
    return roots


def find_card(explicit: str | None = None) -> Path:
    given = explicit or os.environ.get("MT12_CARD")
    if given:
        path = Path(given)
        if not is_card(path):
            raise CardError(f"{path} does not look like an MT12 card (needs RADIO/radio.yml with board: {BOARD} and MODELS/)")
        return path
    found = []
    for root in candidate_roots():
        try:
            if root not in found and is_card(root):
                found.append(root)
        except OSError:
            continue
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise CardError("more than one card found, pick one with --card: " + ", ".join(map(str, found)))
    hint = ""
    if is_wsl():
        hint = ("\nIn WSL, removable drives must be mounted by hand, e.g.:\n"
                "  sudo mkdir -p /mnt/e && sudo mount -t drvfs E: /mnt/e")
    raise CardError("no MT12 card found; insert it or pass --card PATH (or set MT12_CARD)." + hint)


# ---------------------------------------------------------------------- files

def tracked_files(base: Path) -> list[Path]:
    """Relative paths of the files that make up the backup, present under `base`."""
    out = []
    for pattern in ("MODELS/model*.yml", "RADIO/radio.yml", "edgetx.sdcard.version"):
        out += sorted(p.relative_to(base) for p in base.glob(pattern) if p.is_file())
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_bytes(src: Path, dst: Path) -> None:
    """Copy exactly and flush to disk; removable media may be pulled at any time."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    with open(dst, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if hashlib.sha256(dst.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
        raise CardError(f"verification failed after writing {dst}")


def diff(card: Path, repo_radio: Path):
    """Return (changed, only_card, only_repo) as lists of relative paths."""
    on_card, in_repo = set(tracked_files(card)), set(tracked_files(repo_radio))
    changed = sorted(p for p in on_card & in_repo if digest(card / p) != digest(repo_radio / p))
    return changed, sorted(on_card - in_repo), sorted(in_repo - on_card)


# ------------------------------------------------------------------- commands

def cmd_status(card: Path, radio: Path) -> int:
    changed, only_card, only_repo = diff(card, radio)
    print(f"card: {card}")
    for label, items in (("differs from radio/", changed), ("only on card", only_card), ("only in radio/", only_repo)):
        for p in items:
            print(f"  {label}: {p}")
    if not (changed or only_card or only_repo):
        print("  card and radio/ are identical")
    return 0


def git(*args, root=ROOT):
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True)


def cmd_pull(card: Path, radio: Path, use_git: bool, push: bool, root: Path = ROOT) -> int:
    for rel in tracked_files(card):
        copy_bytes(card / rel, radio / rel)
    _, _, only_repo = diff(card, radio)
    for p in only_repo:
        print(f"note: {p} is in radio/ but not on the card (left in place)")
    if not use_git:
        print("copied; review with git diff and commit")
        return 0
    git("add", "radio", root=root)
    if git("diff", "--cached", "--quiet", "--", "radio", root=root).returncode == 0:
        print("no changes since the last backup")
        return 0
    stamp = time.strftime("%Y-%m-%d %H:%M")
    res = git("commit", "-m", f"Backup from card {stamp}", root=root)
    if res.returncode:
        print(res.stderr or res.stdout, file=sys.stderr)
        return 1
    print(f"committed backup ({stamp})")
    if push:
        res = git("push", root=root)
        if res.returncode:
            print("push failed:\n" + (res.stderr or res.stdout), file=sys.stderr)
            return 1
        print("pushed to GitHub")
    return 0


def cmd_push(card: Path, radio: Path, build: Path, force: bool, dry_run: bool, root: Path = ROOT) -> int:
    files = sorted(p.relative_to(build.parent) for p in build.glob("model*.yml"))
    if not files:
        raise CardError(f"nothing to push: no model*.yml in {build}")

    # A card file is safe to overwrite if it matches the last backup or what we last pushed.
    state_file = build.parent / "last_push.json"
    try:
        last_push = json.loads(state_file.read_text())
    except (OSError, ValueError):
        last_push = {}
    problems = []
    for rel in files:
        on_card, backup = card / rel, radio / rel
        if not on_card.exists():
            continue
        known = {last_push.get(rel.as_posix())}
        if backup.exists():
            known.add(digest(backup))
        if digest(on_card) not in known:
            problems.append(f"{rel} on the card differs from radio/ and from the last push (unbacked changes: trims, gvars, edits)")
    newest_radio = max((p.stat().st_mtime for p in radio.glob("MODELS/*.yml")), default=0)
    stale = [rel for rel in files if (build.parent / rel).stat().st_mtime < newest_radio]
    if stale:
        problems.append("build/ is older than radio/ for " + ", ".join(map(str, stale)) + "; regenerate first")
    if problems and not force:
        for p in problems:
            print("refusing: " + p, file=sys.stderr)
        print("run `card.py pull` (and regenerate), or use --force", file=sys.stderr)
        return 2

    saved = build.parent / "previous" / time.strftime("%Y%m%d-%H%M%S")
    for rel in files:
        if dry_run:
            print(f"would write {card / rel}")
            continue
        if (card / rel).exists():
            copy_bytes(card / rel, saved / rel)
        copy_bytes(build.parent / rel, card / rel)
        last_push[rel.as_posix()] = digest(card / rel)
        print(f"wrote {card / rel}")
    if not dry_run:
        state_file.write_text(json.dumps(last_push, indent=2))
        print(f"previous card files saved in {saved}")
        print("eject the card safely before putting it back in the radio")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["status", "pull", "push"])
    ap.add_argument("--card", help="card root, e.g. E:\\ or /mnt/e or /media/me/MT12")
    ap.add_argument("--no-git", action="store_true", help="pull: copy only, no commit")
    ap.add_argument("--no-push", action="store_true", help="pull: commit but do not push to GitHub")
    ap.add_argument("--force", action="store_true", help="push: overwrite even if the card is out of sync")
    ap.add_argument("--dry-run", action="store_true", help="push: show what would be written")
    args = ap.parse_args(argv)
    try:
        card = find_card(args.card)
        radio = ROOT / "radio"
        if args.command == "status":
            return cmd_status(card, radio)
        if args.command == "pull":
            return cmd_pull(card, radio, not args.no_git, not args.no_push)
        return cmd_push(card, radio, ROOT / "build" / "MODELS", args.force, args.dry_run)
    except (CardError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

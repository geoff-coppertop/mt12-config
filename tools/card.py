#!/usr/bin/env python3
"""Sync the MT12 SD card with this repo: back up from the card, push generated models to it.

  uv run tools/card.py status            setting-by-setting diff between card and radio/
  uv run tools/card.py pull              card -> radio/ (you accept or reject each change), commit, push
  uv run tools/card.py push              build/MODELS -> card (you accept or reject each change)

pull and push ask about every changed setting (y/n, like `git add -p`) when run in a
terminal; --all takes everything without asking.

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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import review  # noqa: E402
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


def write_bytes(data: bytes, dst: Path) -> None:
    """Write exactly and flush to disk, then verify by reading back."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if dst.read_bytes() != data:
        raise CardError(f"verification failed after writing {dst}")


def review_file(rel: Path, dest: Path, src: Path, session) -> bytes | None:
    """Ask which changes to take from `src` into `dest`; None if nothing changes."""
    new = src.read_bytes()
    old = dest.read_bytes() if dest.exists() else None
    if old == new:
        return None
    if old is None:
        changes = [review.Change("(whole file is new)", (0, 0), (0, len(review.split_lines(new))))]
        old = b""
    else:
        changes = review.diff(old, new)
    accepted = session.review(rel.as_posix(), old, new, changes)
    if not accepted:
        return None
    return review.apply(old, new, changes, accepted)


def diff(card: Path, repo_radio: Path):
    """Return (changed, only_card, only_repo) as lists of relative paths."""
    on_card, in_repo = set(tracked_files(card)), set(tracked_files(repo_radio))
    changed = sorted(p for p in on_card & in_repo if digest(card / p) != digest(repo_radio / p))
    return changed, sorted(on_card - in_repo), sorted(in_repo - on_card)


# ------------------------------------------------------------------- commands

def cmd_status(card: Path, radio: Path) -> int:
    changed, only_card, only_repo = diff(card, radio)
    print(f"card: {card}")
    names = ("radio/", "card")
    for p in changed:
        review.show_only(p.as_posix(), (radio / p).read_bytes(), (card / p).read_bytes(),
                         review.diff((radio / p).read_bytes(), (card / p).read_bytes()), names)
        print()
    for label, items in (("only on card", only_card), ("only in radio/", only_repo)):
        for p in items:
            print(f"  {label}: {p}")
    if not (changed or only_card or only_repo):
        print("  card and radio/ are identical")
    return 0


def git(*args, root=ROOT):
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True)


def cmd_pull(card: Path, radio: Path, use_git: bool, push: bool, root: Path = ROOT, session=None) -> int:
    partial = False
    if session is None:
        for rel in tracked_files(card):
            copy_bytes(card / rel, radio / rel)
    else:
        results = {}
        try:
            for rel in tracked_files(card):
                data = review_file(rel, radio / rel, card / rel, session)
                if data is not None:
                    results[rel] = data
        except review.ReviewAbort:
            print("\nabandoned; nothing was written")
            return 1
        for rel, data in results.items():
            write_bytes(data, radio / rel)
        left = diff(card, radio)
        partial = bool(left[0] or [p for p in left[1]])
        print(f"\nwrote {len(results)} file(s) into radio/" + ("; some changes left on the card only" if partial else ""))
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
    res = git("commit", "-m", f"{'Partial backup' if partial else 'Backup'} from card {stamp}", root=root)
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


def cmd_push(card: Path, radio: Path, build: Path, force: bool, dry_run: bool, root: Path = ROOT, session=None) -> int:
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
    # When reviewing, card-only edits are no longer a hard stop: they show up as changes
    # you can reject to keep the card's value. A stale build still stops the push.
    reviewing = session is not None and not dry_run
    blocking = [p for p in problems if p.startswith("build/")] if reviewing else problems
    if blocking and not force:
        for p in blocking:
            print("refusing: " + p, file=sys.stderr)
        print("run `card.py pull` (and regenerate), or use --force", file=sys.stderr)
        return 2
    if reviewing:
        for p in problems:
            if p not in blocking:
                print("warning: " + p, file=sys.stderr)
        results = {}
        try:
            for rel in files:
                data = review_file(rel, card / rel, build.parent / rel, session)
                if data is not None:
                    results[rel] = data
        except review.ReviewAbort:
            print("\nabandoned; nothing was written")
            return 1
        files = sorted(results)
    else:
        results = None

    if results is not None and not files:
        print("nothing to write")
        return 0
    saved = build.parent / "previous" / time.strftime("%Y%m%d-%H%M%S")
    for rel in files:
        if dry_run:
            print(f"would write {card / rel}")
            continue
        if (card / rel).exists():
            copy_bytes(card / rel, saved / rel)
        if results is not None:
            write_bytes(results[rel], card / rel)
        else:
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
    ap.add_argument("--all", action="store_true", help="pull/push: take every change without asking")
    ap.add_argument("--dry-run", action="store_true", help="push: show what would be written")
    args = ap.parse_args(argv)
    try:
        card = find_card(args.card)
        radio = ROOT / "radio"
        if args.command == "status":
            return cmd_status(card, radio)
        interactive = sys.stdin.isatty() and not args.all
        if args.command == "pull":
            session = review.Session(("radio/", "card")) if interactive else None
            return cmd_pull(card, radio, not args.no_git, not args.no_push, session=session)
        session = review.Session(("card", "generated")) if interactive else None
        return cmd_push(card, radio, ROOT / "build" / "MODELS", args.force, args.dry_run, session=session)
    except (CardError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

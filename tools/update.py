"""Update the MT12 and its ELRS gear to the versions pinned in versions.yml.

  uv run tools/card.py check            pinned versions against the newest releases
  uv run tools/card.py check --bump     move the pins to the newest releases (review with git diff)
  uv run tools/card.py update           back up, then SD card, EdgeTX firmware, ELRS module, receivers

Every download is checked against the sha256 in versions.yml before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = ROOT / "versions.yml"
CACHE = ROOT / "build" / "cache"
FLASH_BACKUPS = ROOT / "build" / "flash-backups"

STEPS = ("sdcard", "sounds", "lua", "firmware", "module", "receivers")
CARD_STEPS = {"sdcard", "sounds", "lua"}

# MT12 is an STM32F407xG (1 MB flash at 0x08000000, edgetx radio/src/targets/taranis/stm32f4_1m_flash.ld).
# The EdgeTX .bin carries the bootloader in its first sector, so one write replaces both.
FLASH_BASE = 0x08000000
FLASH_SIZE = 1024 * 1024
DFU_ID = "0483:df11"
VCP_ID = (0x0483, 0x5740)  # EdgeTX USB serial (usbd_desc.c USBD_CDC_PID)
ELRS_INDEX = "https://artifactory.expresslrs.org/ExpressLRS/index.json"
ELRS_FIRMWARE = "https://artifactory.expresslrs.org/ExpressLRS/{commit}/firmware.zip"

# Never written to the card from a pack: the radio's own settings, and colour-screen widgets.
SKIP_PREFIXES = ("MODELS/", "RADIO/", "WIDGETS/")


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------------- pins

def load_pins(path: Path = PINS) -> dict:
    import yaml
    pins = yaml.safe_load(path.read_text())
    missing = [k for k in ("edgetx", "sdcard", "sounds", "elrs_lua", "elrs") if k not in pins]
    if missing:
        raise UpdateError(f"{path.name} is missing: {', '.join(missing)}")
    return pins


def save_pins(pins: dict, path: Path = PINS) -> None:
    import yaml
    header = "".join(line for line in path.read_text().splitlines(keepends=True) if line.startswith("#"))
    path.write_text(header + yaml.safe_dump(pins, sort_keys=False, default_flow_style=False, width=1000))


# ----------------------------------------------------------------- downloads

def http_get(url: str) -> bytes:
    headers = {"User-Agent": "mt12-config", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
        return resp.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(entry: dict, cache: Path = CACHE, get=http_get) -> Path:
    """Download entry['url'] once, verify it against entry['sha256'], return the cached file."""
    want = entry.get("sha256")
    if not want:
        raise UpdateError(f"no sha256 pinned for {entry['url']}; run `card.py check --bump` first")
    path = cache / f"{want}.zip"
    if path.exists() and sha256(path.read_bytes()) == want:
        return path
    print(f"downloading {entry['url']}")
    data = get(entry["url"])
    got = sha256(data)
    if got != want:
        raise UpdateError(f"sha256 mismatch for {entry['url']}: pinned {want}, got {got}")
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ SD card

def safe_member(name: str) -> bool:
    parts = Path(name).parts
    return not (name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]))


def merge_zip(archive: Path, card: Path, dry_run: bool = False) -> tuple[int, int]:
    """Copy a pack onto the card, leaving MODELS/, RADIO/ and WIDGETS/ alone. Returns (written, unchanged)."""
    from tools.card import write_bytes
    written = unchanged = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name.startswith(SKIP_PREFIXES):
                continue
            if not safe_member(name):
                raise UpdateError(f"{archive.name}: refusing unsafe path {name}")
            data, dest = z.read(info), card / name
            if dest.exists() and dest.read_bytes() == data:
                unchanged += 1
                continue
            written += 1
            if not dry_run:
                write_bytes(data, dest)
    return written, unchanged


# ----------------------------------------------------------------- firmware

def firmware_image(archive: Path) -> bytes:
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"mt12-[0-9a-f]+\.bin", n)]
        if len(names) != 1:
            raise UpdateError(f"{archive.name}: expected one mt12-*.bin, found {names or 'none'}")
        data = z.read(names[0])
    if not 0 < len(data) <= FLASH_SIZE:
        raise UpdateError(f"{names[0]} is {len(data)} bytes; does not fit the MT12's {FLASH_SIZE} byte flash")
    return data


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def dfu_present(runner=run) -> bool:
    if not shutil.which("dfu-util"):
        raise UpdateError("dfu-util is not on PATH")
    res = runner(["dfu-util", "-l"])
    return f"[{DFU_ID}]" in (res.stdout + res.stderr)


def dfu_commands(image: Path, backup: Path) -> list[list[str]]:
    dev = ["dfu-util", "-d", DFU_ID, "-a", "0"]
    return [dev + ["-s", f"0x{FLASH_BASE:08x}:{FLASH_SIZE}", "-U", str(backup)],
            dev + ["-s", f"0x{FLASH_BASE:08x}:leave", "-D", str(image)]]


def flash_firmware(image: bytes, ask, dry_run: bool, runner=run, stamp: str | None = None) -> None:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    FLASH_BACKUPS.mkdir(parents=True, exist_ok=True)
    image_path = FLASH_BACKUPS / f"{stamp}-new.bin"
    backup = FLASH_BACKUPS / f"{stamp}-previous.bin"
    read_back, write = dfu_commands(image_path, backup)
    if dry_run:
        for cmd in (read_back, write):
            print("would run: " + " ".join(cmd))
        return
    image_path.write_bytes(image)
    ask("Eject the card, unplug USB, turn the radio OFF, then plug USB into the side port (DFU). Enter when done")
    while not dfu_present(runner):
        ask(f"no {DFU_ID} DFU device found; check the cable and that the radio is off. Enter to retry")
    for label, cmd in (("backing up current flash", read_back), ("flashing", write)):
        print(f"{label}: {' '.join(cmd)}")
        res = runner(cmd)
        if res.returncode:
            raise UpdateError(f"dfu-util failed ({label}):\n{res.stdout}{res.stderr}")
    print(f"previous flash saved as {backup}")


# --------------------------------------------------------------------- ELRS

def elrs_tree(archive: Path, cache: Path = CACHE) -> Path:
    """Unpack the ELRS firmware bundle (firmware/ with FCC/, LBT/, hardware/, flasher.pyz) once."""
    out = cache / archive.stem
    if not (out / "firmware" / "flasher.pyz").exists():
        with zipfile.ZipFile(archive) as z:
            bad = [n for n in z.namelist() if not safe_member(n)]
            if bad:
                raise UpdateError(f"{archive.name}: refusing unsafe paths {bad[:3]}")
            z.extractall(out)
    return out / "firmware"


def elrs_command(tree: Path, target: str, method: str, port: str | None, tx: bool, lbt: bool,
                 phrase: str | None) -> list[str]:
    cmd = [sys.executable, str(tree / "flasher.pyz"), "--dir", str(tree), "--target", target, "--flash", method]
    if tx:
        cmd.append("--tx")
    if lbt:
        cmd.append("--lbt")
    if port:
        cmd += ["--port", port]
    if phrase:
        cmd += ["--phrase", phrase]
    return cmd


def vcp_ports() -> list[str]:
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if (p.vid, p.pid) == VCP_ID]


def run_flasher(cmd: list[str], dry_run: bool, runner=subprocess.run) -> None:
    shown = [("<phrase>" if i and cmd[i - 1] == "--phrase" else a) for i, a in enumerate(cmd)]
    print("running: " + " ".join(shown))
    if dry_run:
        return
    if runner(cmd).returncode:
        raise UpdateError("ELRS flasher failed; see its output above")


# ---------------------------------------------------------------- commands

def ask_tty(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise UpdateError("update needs a terminal: it waits for you to replug and power-cycle the radio")
    return input(prompt + ": ").strip().lower()


def cmd_update(steps: list[str], card_arg: str | None, dry_run: bool, git_push: bool, ask=ask_tty) -> int:
    from tools import card as c
    pins = load_pins()
    radio = ROOT / "radio"

    # Verify every download before touching anything.
    files = {}
    for step, key in (("sdcard", "sdcard"), ("sounds", "sounds"), ("lua", "elrs_lua"), ("firmware", "edgetx")):
        if step in steps:
            files[step] = fetch(pins[key])
    if {"module", "receivers"} & set(steps):
        files["elrs"] = fetch(pins["elrs"])
    image = firmware_image(files["firmware"]) if "firmware" in steps else None

    if CARD_STEPS & set(steps) or "firmware" in steps:
        ask("Radio on, USB into the side port, choose USB Storage. Enter when the card is mounted")
        card = c.find_card(card_arg)
        print(f"card: {card}")
        if not dry_run and c.cmd_pull(card, radio, True, git_push, message="Backup before update") not in (0, None):
            raise UpdateError("backup failed; nothing was changed")
        for step in ("sdcard", "sounds", "lua"):
            if step in steps:
                written, same = merge_zip(files[step], card, dry_run)
                print(f"{step}: {written} file(s) {'would be ' if dry_run else ''}written, {same} unchanged")
        if "sdcard" in steps:
            print("note: SCRIPTS/TOOLS/elrs.lua from older installs is left in place; delete it if the new ExpressLRS tool is listed")

    if image is not None:
        flash_firmware(image, ask, dry_run)
        if not dry_run:
            ask("Unplug, turn the radio on, open each model once so EdgeTX converts it, then plug in as USB Storage. Enter when mounted")
            c.cmd_pull(c.find_card(card_arg), radio, True, git_push, message=f"EdgeTX {pins['edgetx']['tag']} migration")

    elrs = pins["elrs"]
    phrase = os.environ.get("MT12_ELRS_PHRASE")
    if {"module", "receivers"} & set(steps):
        if not phrase:
            print("MT12_ELRS_PHRASE is not set: firmware goes on without a bind phrase, so bind with the ExpressLRS Bind tool afterwards")
        print(f"ELRS {elrs['tag']}: different major versions do not connect, so do the module and both receivers in one go")
        tree = elrs_tree(files["elrs"])

    if "module" in steps:
        mod = elrs["module"]
        port = None
        if mod["method"] == "etx" and dry_run:
            port = "<EdgeTX serial port>"
        elif mod["method"] == "etx":
            ask("Radio on; SYS > Hardware > USB-VCP = CLI; plug USB and choose USB Serial (VCP). Enter when connected")
            ports = vcp_ports()
            if len(ports) != 1:
                raise UpdateError(f"expected one EdgeTX serial port ({VCP_ID[0]:04x}:{VCP_ID[1]:04x}), found {ports or 'none'}")
            port = ports[0]
        run_flasher(elrs_command(tree, mod["target"], mod["method"], port, True, elrs.get("lbt", False), phrase), dry_run)

    if "receivers" in steps:
        rx = elrs["receivers"]
        for car in rx.get("cars", ["receiver"]):
            if rx["method"] == "wifi":
                prompt = (f"{car}: transmitter off, power the car, wait about 60 s, join the 'ExpressLRS RX' WiFi "
                          "(password expresslrs). Enter to flash, s to skip")
                port = "10.0.0.1"
            else:
                prompt = f"{car}: connect the receiver to the UART flasher. Enter to flash, s to skip"
                port = None
            if not dry_run and ask(prompt) == "s":
                continue
            run_flasher(elrs_command(tree, rx["target"], rx["method"], port, False, elrs.get("lbt", False), phrase), dry_run)
        print("Rejoin your normal network, then check RxBt telemetry on both cars")
    return 0


# ------------------------------------------------------------------- check

def gh(path: str, get=http_get):
    return json.loads(get(f"https://api.github.com/repos/{path}"))


def asset_entry(release: dict, name: str) -> dict:
    asset = next(a for a in release["assets"] if a["name"] == name)
    digest = asset.get("digest") or ""
    return {"tag": release["tag_name"], "url": asset["browser_download_url"],
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None}


def first_with(releases: list, pick) -> dict:
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        name = pick(rel)
        if name:
            return asset_entry(rel, name)
    raise UpdateError("no stable release with the expected asset")


def semver(tag: str):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else None


def latest(pins: dict, get=http_get) -> dict:
    names = lambda rel: [a["name"] for a in rel["assets"]]  # noqa: E731
    out = {}
    fw = gh("EdgeTX/edgetx/releases/latest", get)
    out["edgetx"] = asset_entry(fw, next(n for n in names(fw) if re.fullmatch(r"edgetx-firmware-v[\d.]+\.zip", n)))
    out["sdcard"] = first_with(gh("EdgeTX/edgetx-sdcard/releases", get),
                               lambda r: "bw128x64.zip" if "bw128x64.zip" in names(r) else None)
    lang = pins["sounds"]["language"]
    def sound(rel):
        want = f"edgetx-sdcard-sounds-{lang}-{rel['tag_name'].lstrip('v')}.zip"
        return want if want in names(rel) else None
    out["sounds"] = dict(first_with(gh("EdgeTX/edgetx-sdcard-sounds/releases", get), sound), language=lang)
    lua = gh("ExpressLRS/Lua/releases/latest", get)
    out["elrs_lua"] = asset_entry(lua, next(n for n in names(lua) if re.fullmatch(r"expresslrs-lua-v[\d.]+\.zip", n)))
    tags = json.loads(get(ELRS_INDEX))["tags"]
    tag = max((t for t in tags if semver(t)), key=semver)
    out["elrs"] = {"tag": tag, "commit": tags[tag], "url": ELRS_FIRMWARE.format(commit=tags[tag]), "sha256": None}
    return out


def cmd_check(bump: bool, get=http_get, path: Path = PINS) -> int:
    pins = load_pins(path)
    new = latest(pins, get)
    stale = []
    for key, entry in new.items():
        cur = pins[key]
        mark = "up to date" if cur["tag"] == entry["tag"] else "newer available"
        if cur["tag"] != entry["tag"] or not cur.get("sha256"):
            stale.append(key)
        print(f"{key:9} pinned {cur['tag']:9} latest {entry['tag']:9} {mark}" + ("" if cur.get("sha256") else " (no sha256 yet)"))
    old_major, new_major = semver(pins["elrs"]["tag"]), semver(new["elrs"]["tag"])
    if old_major and new_major and old_major[0] != new_major[0]:
        print(f"warning: ELRS {old_major[0]}.x -> {new_major[0]}.x: module and receivers must all be flashed together")
    if not bump or not stale:
        return 0
    for key in stale:
        entry = new[key] if pins[key]["tag"] != new[key]["tag"] else {**pins[key], "sha256": None}
        if not entry.get("sha256"):
            print(f"hashing {entry['url']}")
            entry["sha256"] = sha256(get(entry["url"]))
        pins[key].update(entry)
    save_pins(pins, path)
    print(f"updated {path.name}: {', '.join(stale)}; review with git diff before running update")
    return 0

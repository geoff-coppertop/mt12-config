#!/usr/bin/env python3
"""Write battery warning/alarm logic into EdgeTX model files.

Reads radio/MODELS/<name>.yml (a backup of the SD card), splices a two-stage RxBt
alert into the logicalSw and customFn sections, and writes the result to
build/MODELS/model<NN>.yml using the slots in radio/slots.yml. Only those two sections change; every other byte of the file,
including the CRLF line endings the radio writes, is kept as is.

Encodings were checked against EdgeTX source (radio/src/storage/yaml/
yaml_datastructs_funcs.cpp and radio/src/myeeprom.h) and against the existing
alert in the M-07R file:
  logicalSw  def: "tele(<sensor index>),<raw value>"   (raw = volts * 10**prec)
  customFn   def: "<param>,<enabled 0/1>,<repeat>"     (repeat: seconds or 1x)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

TESTED_SEMVER = "2.11.3"
UNIT_VOLTS = 1
LS_WARNING, LS_ALARM = 0, 1  # 0-based slots, shown on the radio as L1 and L2
MAX_CUSTOM_FUNCTIONS = 64
SOUNDS = ["Bp1", "Bp2", "Bp3", "Wrn1", "Wrn2", "Chee", "Rata", "Tick",
          "Sirn", "Ring", "SciF", "Robt", "Chrp", "Tada", "Crck", "Alrm"]

TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
ENTRY = re.compile(r"^   (\d+):\s*$")


class GenError(Exception):
    pass


# ---------------------------------------------------------------- file layout

def top_blocks(lines):
    """Map each top-level key to (start, end) line indexes, end exclusive."""
    starts = [(i, TOP_KEY.match(l).group(1)) for i, l in enumerate(lines) if TOP_KEY.match(l)]
    out = {}
    for n, (i, key) in enumerate(starts):
        out[key] = (i, starts[n + 1][0] if n + 1 < len(starts) else len(lines))
    return out


def split_entries(body):
    """Split the lines under a section key into {slot: [lines]}."""
    entries, cur = {}, None
    for line in body:
        m = ENTRY.match(line)
        if m:
            cur = int(m.group(1))
            entries[cur] = [line]
        elif cur is None:
            raise GenError(f"unexpected line in section: {line!r}")
        else:
            entries[cur].append(line)
    return entries


def put_section(lines, key, entries, before):
    """Replace section `key`, or insert it in front of the first of `before`."""
    block = [f"{key}: "]  # the radio writes a trailing space after the key
    for slot in sorted(entries):
        block += entries[slot]
    blocks = top_blocks(lines)
    if key in blocks:
        s, e = blocks[key]
        lines[s:e] = block
        return
    anchors = [blocks[k][0] for k in before if k in blocks]
    if not anchors:
        raise GenError(f"nowhere to insert {key}: none of {before} present")
    pos = min(anchors)
    lines[pos:pos] = block


# ------------------------------------------------------------------ rendering

def ls_entry(slot, raw, delay, andsw):
    return [
        f"   {slot}:",
        "      func: FUNC_VNEG",  # a < x
        f'      def: "tele({{sensor}}),{raw}"',
        f'      andsw: "{andsw}"',
        "      lsPersist: 0",
        "      lsState: 0",
        f"      delay: {delay}",
        "      duration: 0",
    ]


def repeat_str(rep):
    if rep == "once":
        return "1x"
    if isinstance(rep, int) and 1 <= rep <= 127:
        return str(rep)
    raise GenError(f"repeat_s must be 'once' or 1-127, got {rep!r}")


def cf_entry(slot, switch, action):
    kind = action["type"]
    rep = repeat_str(action.get("repeat_s", "once"))
    if kind == "haptic":
        func, param = "HAPTIC", str(int(action["pattern"]))
    elif kind == "sound":
        if action["name"] not in SOUNDS:
            raise GenError(f"unknown sound {action['name']!r}; choose from {SOUNDS}")
        func, param = "PLAY_SOUND", action["name"]
    elif kind == "track":
        func, param = "PLAY_TRACK", str(action["name"])
    else:
        raise GenError(f"unknown action type {kind!r}")
    return [f"   {slot}:", f'      swtch: "{switch}"', f"      func: {func}", f'      def: "{param},1,{rep}"']


# --------------------------------------------------------------------- engine

def sensor_index(data, label):
    for idx, s in (data.get("telemetrySensors") or {}).items():
        if s.get("label") == label:
            if s.get("unit") != UNIT_VOLTS:
                raise GenError(f"sensor {label} has unit {s.get('unit')}, expected volts ({UNIT_VOLTS})")
            return idx, int(s.get("prec", 0))
    raise GenError(f"sensor {label!r} not found in telemetrySensors")


def stage(profile, name):
    p = profile[name]
    if not 0 <= p["delay_s"] <= 25.5:
        raise GenError(f"{name} delay_s must be 0-25.5")
    return p["volts"], int(round(p["delay_s"] * 10))


def generate(text, cfg, model_cfg, notes):
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    data = yaml.safe_load(text)

    if str(data.get("semver")) != TESTED_SEMVER:
        notes.append(f"warning: file is EdgeTX {data.get('semver')}, generator tested on {TESTED_SEMVER}")

    profile_name = model_cfg.get("profile", cfg["active_profile"])
    profile = cfg["profiles"][profile_name]
    (w_v, w_d), (a_v, a_d) = stage(profile, "warning"), stage(profile, "alarm")
    if not w_v > a_v:
        raise GenError("warning voltage must be above alarm voltage")

    sensor, prec = sensor_index(data, cfg["sensor"])
    raw = lambda v: int(round(v * 10 ** prec))

    def sw(slot):
        return f"L{slot + 1}"

    ls = ls_entry(LS_WARNING, raw(w_v), w_d, "!" + sw(LS_ALARM))
    ls_alarm = ls_entry(LS_ALARM, raw(a_v), a_d, "NONE")
    ls = [l.replace("{sensor}", str(sensor)) for l in ls]
    ls_alarm = [l.replace("{sensor}", str(sensor)) for l in ls_alarm]
    new_ls = {LS_WARNING: ls, LS_ALARM: ls_alarm}

    first = model_cfg["first_function_slot"]
    new_cf, slot = {}, first
    for stage_name, switch in (("warning", sw(LS_WARNING)), ("alarm", sw(LS_ALARM))):
        for action in cfg["actions"][stage_name]:
            new_cf[slot] = cf_entry(slot, switch, action)
            slot += 1
    if slot > MAX_CUSTOM_FUNCTIONS:
        raise GenError("too many special functions")

    blocks = top_blocks(lines)

    def existing(key):
        if key not in blocks:
            return {}
        s, e = blocks[key]
        return split_entries(lines[s + 1:e])

    old_ls, old_cf = existing("logicalSw"), existing("customFn")
    owned_ls = {s: e for s, e in old_ls.items() if s in new_ls}
    owned_cf = {s: e for s, e in old_cf.items() if s >= first}

    clobbered = [(f"L{s + 1}", e) for s, e in owned_ls.items() if e != new_ls[s]]
    clobbered += [(f"SF{s + 1}", e) for s, e in owned_cf.items() if e != new_cf.get(s)]
    if clobbered:
        if not model_cfg.get("replace_existing"):
            names = ", ".join(n for n, _ in clobbered)
            raise GenError(f"would overwrite existing {names}; set replace_existing: true to allow")
        for name, entry in clobbered:
            notes.append(f"replaced {name}: " + " ".join(l.strip() for l in entry))

    kept_ls = {s: e for s, e in old_ls.items() if s not in new_ls}
    kept_cf = {s: e for s, e in old_cf.items() if s < first}

    put_section(lines, "logicalSw", {**kept_ls, **new_ls}, ["customFn", "flightModeData", "thrTraceSrc"])
    put_section(lines, "customFn", {**kept_cf, **new_cf}, ["flightModeData", "thrTraceSrc"])

    notes.append(
        f"profile {profile_name}: warning < {w_v:.1f} V for {w_d / 10:g} s (L1), "
        f"alarm < {a_v:.1f} V for {a_d / 10:g} s (L2), sensor tele({sensor}), "
        f"{len(new_cf)} special functions from SF{first + 1}"
    )
    return eol.join(lines), kept_ls, kept_cf


def verify(original, generated, kept_ls, kept_cf):
    """Parse both files and confirm only the two intended sections differ."""
    a, b = yaml.safe_load(original), yaml.safe_load(generated)
    for key in set(a) | set(b):
        if key in ("logicalSw", "customFn"):
            continue
        if a.get(key) != b.get(key):
            raise GenError(f"unexpected change in section {key}")
    for key, kept in (("logicalSw", kept_ls), ("customFn", kept_cf)):
        for slot in kept:
            if (a.get(key) or {}).get(slot) != (b.get(key) or {}).get(slot):
                raise GenError(f"{key}[{slot}] changed but should have been kept")


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=root / "alerts" / "alerts.yml")
    ap.add_argument("--src", type=Path, default=root / "radio" / "MODELS")
    ap.add_argument("--slots", type=Path, default=root / "radio" / "slots.yml")
    ap.add_argument("--out", type=Path, default=root / "build" / "MODELS")
    ap.add_argument("--profile", help="override active_profile for every model")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    if args.profile:
        cfg["active_profile"] = args.profile
    if cfg["active_profile"] not in cfg["profiles"]:
        sys.exit(f"unknown profile {cfg['active_profile']!r}")

    slots = yaml.safe_load(args.slots.read_text())
    failed = False
    for slug, model_cfg in cfg["models"].items():
        model_cfg = model_cfg or {}
        if slug not in slots:
            print(f"{slug}: ERROR not listed in {args.slots}", file=sys.stderr)
            failed = True
            continue
        fname = f"model{slots[slug]:02d}.yml"
        src = args.src / f"{slug}.yml"
        try:
            text = src.read_bytes().decode("utf-8")
            notes = []
            out, kept_ls, kept_cf = generate(text, cfg, {**model_cfg, **({"profile": args.profile} if args.profile else {})}, notes)
            verify(text, out, kept_ls, kept_cf)
        except (GenError, OSError, KeyError, yaml.YAMLError) as exc:
            print(f"{fname}: ERROR {exc}", file=sys.stderr)
            failed = True
            continue
        name = (yaml.safe_load(text).get("header") or {}).get("name", "?")
        print(f"{fname} ({name})")
        for n in notes:
            print(f"  {n}")
        if not args.dry_run:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / fname).write_bytes(out.encode("utf-8"))
            print(f"  wrote {args.out / fname}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Write battery warning/alarm logic into EdgeTX model files.

Reads radio/MODELS/*.yml (a backup of the SD card), splices a two-stage RxBt
alert into the logicalSw and customFn sections, and writes the result to
build/MODELS/. Only those two sections change; every other byte of the file,
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


# ------------------------------------------------------------ gvar mix porting

GVAR_MAX = 1024  # radio/src/gvars.h; a gvar's range is [-GVAR_MAX + min, GVAR_MAX - max]


def set_block(lines, key, block, before):
    """Replace top-level section `key` with raw `block` lines, or insert it."""
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


def port_gvar_mix(lines, src_text, pcfg, notes):
    """Copy the trim -> gvar mixing of another model into this one.

    Copies: thrTrim/displayTrims/trimInc, the expoData lines that read the
    gvars, the gvars definitions, the ADJUST_GVAR special functions and a
    flightModeData block with trims zeroed and gvars at their start values.
    Runs only when the target has no gvars yet, so a later backup that already
    contains the setup is left alone.
    """
    eol = "\r\n" if "\r\n" in "\n".join(lines) else "\n"
    if "gvars" in top_blocks(lines):
        notes.append("gvar mixing already present, left alone")
        return
    src = src_text.split("\r\n" if "\r\n" in src_text else "\n")
    sb, tb = top_blocks(src), top_blocks(lines)
    sdata, tdata = yaml.safe_load(src_text), yaml.safe_load(eol.join(lines))
    for key in ("gvars", "expoData", "flightModeData", "customFn"):
        if key not in sb:
            raise GenError(f"source model has no {key}")
    if "flightModeData" in tb:
        raise GenError("target has flightModeData but no gvars; port by hand")
    if sdata["mixData"] != tdata["mixData"] or sdata.get("inputNames") != tdata.get("inputNames"):
        raise GenError("mixData or inputNames differ between source and target; port by hand")

    def section(key):
        s, e = sb[key]
        return src[s:e]

    # trim behaviour: copy the three scalar lines verbatim
    for key in ("thrTrim", "displayTrims", "trimInc"):
        lines[top_blocks(lines)[key][0]] = src[sb[key][0]]

    # expoData: source lines, with the steering expo curve set as configured
    expo, in_st = section("expoData"), False
    for i, line in enumerate(expo):
        if line == " -":
            in_st = False
        elif line.strip() == 'srcRaw: "ST"':
            in_st = True
        elif in_st and re.match(r"^      value: -?\d+$", line):
            expo[i] = f"      value: {int(pcfg.get('steering_expo', 0))}"
    set_block(lines, "expoData", expo, ["thrTraceSrc"])

    # gvar definitions, in the position the radio writes them
    set_block(lines, "gvars", section("gvars"), ["rssiSource", "rfAlarms", "thrTrimSw"])

    # trim -> gvar adjusters keep their slots
    cf = split_entries(section("customFn")[1:])
    adj = {slot: e for slot, e in cf.items() if any("func: ADJUST_GVAR" in l for l in e)}
    first = pcfg["first_function_slot"]
    if adj and max(adj) >= first:
        raise GenError(f"first_function_slot {first} collides with adjuster slot {max(adj)}")
    block = ["customFn: "]
    for slot in sorted(adj):
        block += adj[slot]
    set_block(lines, "customFn", block, ["flightModeData", "thrTraceSrc"])

    # flightModeData: trims zero, gvars at start values
    names = {i: g for i, g in sdata["gvars"].items()}
    start = pcfg["start_values"]
    values = {}
    for i, g in names.items():
        if g["name"] not in start:
            raise GenError(f"no start value for gvar {g['name']}")
        v = int(round(start[g["name"]] * (10 if g.get("prec") else 1)))
        lo, hi = -GVAR_MAX + g["min"], GVAR_MAX - g["max"]
        if not lo <= v <= hi:
            raise GenError(f"start value {start[g['name']]} for {g['name']} outside its range")
        values[i] = v
    fm, mode, gi = section("flightModeData"), None, None
    for i, line in enumerate(fm):
        if line.startswith("      trim:"):
            mode = "trim"
        elif line.startswith("      gvars:"):
            mode = "gvars"
        elif re.match(r"^      \w", line):
            mode = None
        m = re.match(r"^         (\d+):$", line)
        if m:
            gi = int(m.group(1))
        if mode == "trim" and re.match(r"^            value: -?\d+$", line):
            fm[i] = "            value: 0"
        elif mode == "gvars" and re.match(r"^            val: -?\d+$", line):
            fm[i] = f"            val: {values.get(gi, 0)}"
    set_block(lines, "flightModeData", fm, ["thrTraceSrc"])
    notes.append(
        "ported gvar mixing (" + ", ".join(g["name"] for g in names.values())
        + f"), trims zeroed, steering expo {int(pcfg.get('steering_expo', 0))}"
    )


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


def generate(text, cfg, model_cfg, notes, src_dir=None):
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

    pcfg = model_cfg.get("port_gvar_mix")
    if pcfg:
        src_dir = src_dir or Path(__file__).resolve().parent.parent / "radio" / "MODELS"
        port_gvar_mix(lines, (src_dir / pcfg["from"]).read_bytes().decode("utf-8"),
                      {**pcfg, "first_function_slot": first}, notes)

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
    return eol.join(lines), kept_ls, kept_cf, bool(pcfg)


PORTED = {"thrTrim", "displayTrims", "trimInc", "expoData", "gvars", "flightModeData"}


def verify(original, generated, kept_ls, kept_cf, ported=False):
    """Parse both files and confirm only the intended sections differ."""
    a, b = yaml.safe_load(original), yaml.safe_load(generated)
    for key in set(a) | set(b):
        if key in ("logicalSw", "customFn") or (ported and key in PORTED):
            continue
        if a.get(key) != b.get(key):
            raise GenError(f"unexpected change in section {key}")
    for key, kept in (("logicalSw", kept_ls), ("customFn", kept_cf)):
        for slot in kept:
            if ported and slot not in (a.get(key) or {}):
                continue  # added by the gvar port, not present in the original
            if (a.get(key) or {}).get(slot) != (b.get(key) or {}).get(slot):
                raise GenError(f"{key}[{slot}] changed but should have been kept")


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=root / "alerts" / "alerts.yml")
    ap.add_argument("--src", type=Path, default=root / "radio" / "MODELS")
    ap.add_argument("--out", type=Path, default=root / "build" / "MODELS")
    ap.add_argument("--profile", help="override active_profile for every model")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    if args.profile:
        cfg["active_profile"] = args.profile
    if cfg["active_profile"] not in cfg["profiles"]:
        sys.exit(f"unknown profile {cfg['active_profile']!r}")

    failed = False
    for fname, model_cfg in cfg["models"].items():
        model_cfg = model_cfg or {}
        src = args.src / fname
        try:
            text = src.read_bytes().decode("utf-8")
            notes = []
            out, kept_ls, kept_cf, ported = generate(
                text, cfg, {**model_cfg, **({"profile": args.profile} if args.profile else {})}, notes, args.src)
            verify(text, out, kept_ls, kept_cf, ported)
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

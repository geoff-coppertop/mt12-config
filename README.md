# mt12-config

Backup of a RadioMaster MT12 (EdgeTX 2.11.3) SD card config, plus a generator
for RxBt battery warning/alarm alerts on the two ER3C-i cars (M-07R and BD-8).

```
radio/            byte-exact copy of the card: MODELS/, RADIO/radio.yml, version file
alerts/alerts.yml thresholds, profiles, actions, per-model settings
alerts/gen_alerts.py  reads radio/MODELS, writes build/MODELS (gitignored)
```

Not tracked: `SCRIPTS/` (stock), `SOUNDS/` (stock pack), `LOGS/`, `SCREENSHOTS/`,
`FIRMWARE/`, `BACKUP/`. Pictures and sounds (`*.wav *.mp3 *.bmp *.png *.jpg`)
are routed through Git LFS by `.gitattributes`, so install `git-lfs` before
adding any. `*.yml` is kept byte-exact because the radio writes CRLF.

## Back up

Copy `MODELS/`, `RADIO/` and `edgetx.sdcard.version` from the card into
`radio/`, review `git diff`, commit. Do this **right before** generating: the
model files also hold trims and gvars (the M-07R adjusts gvars from trims), so
an old backup would overwrite recent changes when copied back.

## Generate and apply alerts

Needs [uv](https://docs.astral.sh/uv/); it creates the Python environment from
`pyproject.toml` and `uv.lock` on first run, nothing to install by hand.

```
uv run alerts/gen_alerts.py --dry-run      # report only
uv run alerts/gen_alerts.py                # writes build/MODELS/model01.yml and model02.yml
uv run alerts/gen_alerts.py --profile practice
uv run python -m unittest alerts.test_gen_alerts
```

Copy the two files from `build/MODELS/` to `MODELS/` on the card, then open
each model on the radio (or in EdgeTX Companion) and check Logical Switches and
Special Functions before relying on it. To test, temporarily raise the warning
voltage above a fresh pack's voltage and confirm it fires after the delay.

The BD-8 also gets a one-time port of the M-07R's gvar mixing (Str, Thr, BrO, BrW
adjusted from trims T2, T1, T4, expo lines, trim settings): gvars start at 100 /
100 / 0 / 100, trims at zero, steering expo 0. It is skipped once the file already
has gvars, so later backups are not touched. Settings are under `port_gvar_mix`
in `alerts/alerts.yml`.

The generator owns L1 (warning), L2 (alarm) and special functions from
`first_function_slot` upward; everything else in each file is untouched, and
it refuses to overwrite existing entries unless `replace_existing: true`.
BD-8 alerts start at SF4, after the three adjusters. On the M-07R it replaces the old `RxBt < 7.4 V` switch and the PLAY_VALUE
function on SF4.

Defaults (race): warning under 7.0 V for 5 s, haptic every 10 s; alarm under
6.6 V for 3 s, haptic plus beep every 4 s. Practice: 7.2 V / 6.8 V. If
telemetry is not streaming (car off) the switches stay false, per EdgeTX
`switches.cpp`, so there are no alarms with only the transmitter on.

## What is verified and what is not

Checked against EdgeTX source: `tele(n)` is a 0-based sensor index; logical
switch value is stored raw (RxBt has one decimal, so 7.0 V is `70`); special
function `def` is `param,enabled,repeat` with repeat in whole seconds
(`CFN_PLAY_REPEAT_MUL` is 1); the sound names; haptic and sound support repeat.

Inferred, not proven: logical switch `delay` is in 0.1 s units (taken from the
existing M-07R value of 50); which haptic pattern (0-3) is longest. The output
has not been loaded on a radio or in Companion.

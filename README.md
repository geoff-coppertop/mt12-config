# mt12-config

Backup of a RadioMaster MT12 (EdgeTX 2.11.3) SD card config, plus a generator
for RxBt battery warning/alarm alerts on the two ER3C-i cars (M-07R and BD-8).

```
radio/            byte-exact copy of the card: MODELS/, RADIO/radio.yml, version file
alerts/alerts.yml thresholds, profiles, actions, per-model settings
alerts/gen_alerts.py  reads radio/MODELS, writes build/MODELS (gitignored)
tools/card.py     pull from / push to the SD card
```

Not tracked: `SCRIPTS/` (stock), `SOUNDS/` (stock pack), `LOGS/`, `SCREENSHOTS/`,
`FIRMWARE/`, `BACKUP/`. Pictures and sounds (`*.wav *.mp3 *.bmp *.png *.jpg`)
are routed through Git LFS by `.gitattributes`, so install `git-lfs` before
adding any. `*.yml` is kept byte-exact because the radio writes CRLF.

## Sync with the card

`tools/card.py` reads from and writes to the card directly. It finds the card by
itself (Windows drive letters, WSL `/mnt/<letter>`, Linux `/media/$USER/*` and
`/run/media/$USER/*`) by looking for `RADIO/radio.yml` with `board: mt12`. Override
with `--card PATH` (`E:\`, `/mnt/e`, `/media/me/MT12`) or the `MT12_CARD` variable.
Files are copied as bytes, so CRLF survives, and `.gitattributes` stops Windows
git (`autocrlf`) from rewriting them.

```
uv run tools/card.py status     # what differs between card and radio/
uv run tools/card.py pull       # card -> radio/, commit "Backup from card <time>", push to GitHub
uv run tools/card.py push       # build/MODELS -> card
```

`status` shows what changed setting by setting (`mixData[2].weight`, `trimInc`), with
the old value in red (`-`) and the new in green (`+`). `pull` and `push` walk through
those same changes in a terminal and ask `y`/`n` for each, like `git add -p`
(`a` take the rest of the file, `r` reject it, `A`/`N` all/none of the remaining files,
`q` quit without writing). Only accepted changes are spliced in, so the files stay
byte-exact. On `push`, trims or gvars changed on the card show up as changes you can
reject to keep. `--all` skips the questions. Mixes inserted in the middle of a list
may be shown as a block of line changes.

`pull` options: `--no-push` (commit only), `--no-git` (copy only). It does nothing
if the card matches the last backup. `push` options: `--dry-run`, `--force`.

**Race tweak, then backup:** `pull`.
**Tweak, then push:** `pull` (so the repo has the latest trims and gvars), edit
`alerts/alerts.yml`, `uv run alerts/gen_alerts.py`, `push`, then eject the card.

`push` refuses when a card file differs from both the last backup and the last
push (changes made on the radio that would be lost), and when `build/` is older
than `radio/` (regenerate first). It saves the files it replaces in
`build/previous/<time>/` and re-reads each file after writing it.

WSL does not mount removable drives by itself. If no card is found, mount it once
per insertion: `sudo mkdir -p /mnt/e && sudo mount -t drvfs E: /mnt/e` (see
[Microsoft's WSL file system post](https://learn.microsoft.com/en-us/archive/blogs/wsl/file-system-improvements-to-the-windows-subsystem-for-linux);
whether a card inserted after WSL started can be mounted this way is untested).
Not yet tried on a real card, Windows or WSL; the logic is covered by tests on
temporary folders.

## Generate and apply alerts

Needs [uv](https://docs.astral.sh/uv/); it creates the Python environment from
`pyproject.toml` and `uv.lock` on first run, nothing to install by hand.

```
uv run alerts/gen_alerts.py --dry-run      # report only
uv run alerts/gen_alerts.py                # writes build/MODELS/model01.yml and model02.yml
uv run alerts/gen_alerts.py --profile practice
uv run python -m unittest alerts.test_gen_alerts tools.test_card
```

Push them with `uv run tools/card.py push` (or copy `build/MODELS/*` to `MODELS/` on the card), then open
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

## Notes on the copied gvar mixing

The steering expo line on both cars has `trimSource: -3`: per EdgeTX
`mixer.cpp` that is trim index 2 (`-value - 1`), not the stick's own trim (0) and
not off (1). It differs from the trims the gvar adjusters read (`T2`, `T1`,
`T4` = indexes 1, 0, 3). Which physical MT12 trim index 2 is was not
determined; check the Trim field on the ST line under Inputs on the radio.

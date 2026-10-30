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

`status` has two parts: what is on the radio but not yet backed up (what `pull` would take), and what the generated `build/MODELS` would change on the card (what `push` would do; run `alerts/gen_alerts.py` first so `build/` is current). It shows what changed setting by setting (`mixData[2].weight`, `trimInc`), with
the `computer` value (the copy in `radio/`) in red (`-`) and the `radio` value (from the card) in green (`+`). On `push` it is the other way round: `radio` is the card's current file, `computer` is the generated one. `pull` and `push` walk through
those same changes in a terminal and ask `y`/`n` for each, like `git add -p`
(`a` take the rest of the file, `r` reject it, `A`/`N` all/none of the remaining files,
`q` quit without writing). Only accepted changes are spliced in, so the files stay
byte-exact. On `push`, trims or gvars changed on the card show up as changes you can
reject to keep. The review names the generator's own switches and functions by their role from `alerts/alerts.yml`, e.g. `L1 (warning): RxBt < 7.0 V and not L2` and `SF6: alarm sound (Wrn1)`.

`checksum` and `globalTimer` in `radio.yml` are rewritten by the radio itself, so they are never asked about: they come along when you accept any other change in that file, and a file where only they differ counts as unchanged. `--all` skips the questions for one run. To turn them off for good, per command, put
this in `card.yml` at the repo root (`--review` asks anyway; `status` always shows the diff):

```yaml
review:
  pull: false   # take everything from the card without asking
  push: true
```

Turning review off for `push` is the same as `--all`: the refusals for unbacked card
changes and a stale `build/` still apply (`--force` overrides them). Mixes inserted in the middle of a list
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

## Update firmware

`versions.yml` pins what gets installed: EdgeTX firmware, the `bw128x64` SD card pack, the sound pack, the ExpressLRS Lua tools, and ExpressLRS firmware for the internal module and both ER3C-i receivers. Every download is checked against its pinned sha256 before anything is written.

```
uv run tools/card.py check                 # pinned vs newest stable releases
uv run tools/card.py check --bump          # move the pins (and fill missing hashes); review with git diff
uv run tools/card.py update --dry-run      # download and verify, print what would happen
uv run tools/card.py update                # all steps
uv run tools/card.py update --only sdcard,sounds,lua,firmware
```

`update` runs in order, prompting you to replug or power-cycle the radio between steps:

1. **Backup:** `pull` with every card change taken, committed as "Backup before update".
2. **sdcard, sounds, lua:** pack files are copied onto the card, never into `MODELS/`, `RADIO/` or `WIDGETS/` (the MT12 has no widgets). Nothing is deleted, so an old `SCRIPTS/TOOLS/elrs.lua` stays until you remove it.
3. **firmware:** radio off, USB into the side port (DFU). It waits for `0483:df11`, saves the whole 1 MB flash to `build/flash-backups/<time>-previous.bin` with `dfu-util -U`, then writes the `mt12-*.bin` at `0x08000000`. The image includes the bootloader. Then you boot the radio, open each model once, and it pulls again as "EdgeTX <tag> migration", so model conversion is its own commit.
4. **module:** ExpressLRS through EdgeTX passthrough. Set SYS > Hardware > USB-VCP to CLI and choose USB Serial (VCP); it finds the `0483:5740` port itself.
5. **receivers:** for each car over WiFi (`10.0.0.1`, after joining the receiver's `ExpressLRS RX` hotspot), or set `method: uart` for the UART flasher dongle. `s` skips a car.

ELRS uses the official bundle (`firmware.zip` from the ExpressLRS artifactory, the same one their Configurator and web flasher use) and its `flasher.pyz`, with targets `radiomaster.tx_2400.mt12` and `radiomaster.rx_2400.er3` and FCC firmware unless `lbt: true`. Set `MT12_ELRS_PHRASE` in the environment to bake in a bind phrase. Without it, bind with the ExpressLRS Bind tool afterwards. Major ELRS versions do not talk to each other, so do the module and both receivers in one session. If you use Model Config Matching, export the module config from its WebUI first; 4.x moved the flash filesystem.

The ELRS sha256 is empty until the first `check --bump`, which needs `artifactory.expresslrs.org`. `update` refuses the module and receiver steps until then.

Needs `dfu-util` on PATH and USB access to the DFU device and serial ports (on NixOS, `custom.edgetx.enable` in `nixos-config`). Linux only so far. On WSL, USB devices need `usbipd`, which has not been tried. None of this has been run against the radio yet: the logic is covered by `tools/test_update.py`, and the firmware and pack handling was checked against the real v2.12.5, v2.12.3 and v1.0.0 downloads.

## Generate and apply alerts

Needs [uv](https://docs.astral.sh/uv/); it creates the Python environment from
`pyproject.toml` and `uv.lock` on first run, nothing to install by hand.

```
uv run alerts/gen_alerts.py --dry-run      # report only
uv run alerts/gen_alerts.py                # writes build/MODELS/model01.yml and model02.yml
uv run alerts/gen_alerts.py --profile practice
uv run python -m unittest alerts.test_gen_alerts tools.test_card tools.test_update
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

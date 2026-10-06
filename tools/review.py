"""Setting-level diff and interactive review for EdgeTX YAML files.

A file is compared as parsed YAML, so a change shows up as `mixData[2].weight`
rather than a line number. Each change knows the exact lines it occupies on both
sides, so accepted changes are spliced into the destination text and every other
byte (CRLF included) stays as it was. Text that is not valid YAML falls back to
plain line hunks.
"""
from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass

import yaml

CONTEXT_KEYS = ("name", "destCh", "chn", "srcRaw", "swtch")


class ReviewAbort(Exception):
    pass


@dataclass
class Change:
    path: str
    dest: tuple[int, int]   # lines [lo, hi) replaced in the destination
    src: tuple[int, int]    # lines [lo, hi) taken from the source
    context: str = ""

    @property
    def kind(self) -> str:
        if self.dest[0] == self.dest[1]:
            return "add"
        if self.src[0] == self.src[1]:
            return "remove"
        return "change"


def split_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    return re.split(r"(?<=\n)", text) if text else []


# ------------------------------------------------------------------ yaml walk

def _end(node, nlines: int) -> int:
    """Exclusive end line of a node."""
    e = node.end_mark
    if isinstance(node, yaml.ScalarNode):
        if node.style in ("|", ">"):
            return e.line
        if node.value == "" and node.style is None:
            return node.start_mark.line + 1
        return e.line + 1 if e.column > 0 else e.line
    return min(e.line, nlines)


def _item_start(node, lines: list[str]) -> int:
    i = node.start_mark.line
    while i > 0 and not lines[i].lstrip().startswith("-"):
        i -= 1
    return i


def _scalars(node) -> dict[str, str]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: v.value for k, v in node.value
            if isinstance(k, yaml.ScalarNode) and isinstance(v, yaml.ScalarNode)}


def _context(node) -> str:
    s = _scalars(node)
    return " ".join(f"{k}={s[k]}" for k in CONTEXT_KEYS if k in s and s[k] != "")


def _plain(node):
    return yaml.safe_load(yaml.serialize(node)) if node is not None else None


def _walk(d, s, path, dl, sl, ctx, out):
    flow = getattr(d, "flow_style", False) or getattr(s, "flow_style", False)
    if isinstance(d, yaml.MappingNode) and isinstance(s, yaml.MappingNode) and not flow:
        dmap = {k.value: (k, v) for k, v in d.value}
        smap = {k.value: (k, v) for k, v in s.value}
        prev = None  # previous source key that also exists in dest
        for name, (sk, sv) in smap.items():
            sub = f"{path}.{name}" if path else str(name)
            if name in dmap:
                _walk(dmap[name][1], sv, sub, dl, sl, ctx, out)
                prev = name
            else:
                if prev is not None:
                    at = _end(dmap[prev][1], len(dl))
                else:
                    at = d.value[0][0].start_mark.line if d.value else _end(d, len(dl))
                out.append(Change(sub, (at, at), (sk.start_mark.line, _end(sv, len(sl))), ctx))
        for name, (dk, dv) in dmap.items():
            if name not in smap:
                sub = f"{path}.{name}" if path else str(name)
                out.append(Change(sub, (dk.start_mark.line, _end(dv, len(dl))), (0, 0), ctx))
    elif isinstance(d, yaml.SequenceNode) and isinstance(s, yaml.SequenceNode) and not flow:
        def text(node, lines):
            return "".join(lines[_item_start(node, lines):_end(node, len(lines))])
        dt = [text(n, dl) for n in d.value]
        st_ = [text(n, sl) for n in s.value]

        def add(j):
            item = s.value[j]
            i = bisect_i
            at = _item_start(d.value[i], dl) if i < len(d.value) else _end(d, len(dl))
            out.append(Change(f"{path}[{j}]", (at, at), (_item_start(item, sl), _end(item, len(sl))),
                              _context(item) or ctx))

        def remove(i):
            item = d.value[i]
            out.append(Change(f"{path}[{i}]", (_item_start(item, dl), _end(item, len(dl))), (0, 0),
                              _context(item) or ctx))

        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, dt, st_, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            n = min(i2 - i1, j2 - j1) if tag == "replace" else 0
            for k in range(n):
                c = _context(s.value[j1 + k]) or ctx
                _walk(d.value[i1 + k], s.value[j1 + k], f"{path}[{j1 + k}]", dl, sl, c, out)
            for i in range(i1 + n, i2):
                remove(i)
            for j in range(j1 + n, j2):
                bisect_i = i2
                add(j)
    else:
        if type(d) is type(s) and isinstance(d, yaml.ScalarNode):
            same = d.value == s.value
        else:
            same = _plain(d) == _plain(s)
        if not same:
            ds = _span_of(d, dl, path)
            ss = _span_of(s, sl, path)
            out.append(Change(path, ds, ss, ctx))


def _span_of(node, lines, path):
    """Lines of a value; for a mapping value this includes the key line when it is a plain scalar."""
    lo = node.start_mark.line
    if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style:
        # block value: the key sits on the line above the first child
        lo = max(lo - 1, 0) if not lines[lo].lstrip().startswith("-") else lo
        while lo > 0 and not re.match(r"\s*(-|[^\s#].*?:\s*$)", lines[lo]):
            lo -= 1
    return lo, _end(node, len(lines))


def _hunks(dl: list[str], sl: list[str]) -> list[Change]:
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, dl, sl, autojunk=False).get_opcodes():
        if tag != "equal":
            span = f"line {i1 + 1}" if i2 - i1 <= 1 else f"lines {i1 + 1}-{i2}"
            out.append(Change(span, (i1, i2), (j1, j2)))
    return out


def diff(dest: bytes, src: bytes) -> list[Change]:
    """Changes needed to turn `dest` into `src`, most specific first."""
    if dest == src:
        return []
    dl, sl = split_lines(dest), split_lines(src)
    try:
        dnode = yaml.compose(dest.decode("utf-8"), Loader=yaml.SafeLoader)
        snode = yaml.compose(src.decode("utf-8"), Loader=yaml.SafeLoader)
        if not isinstance(dnode, yaml.MappingNode) or not isinstance(snode, yaml.MappingNode):
            raise ValueError("not a mapping")
        changes: list[Change] = []
        _walk(dnode, snode, "", dl, sl, "", changes)
        # Safety net: the changes must reproduce the source exactly when all are applied.
        if changes and apply(dest, src, changes, set(range(len(changes)))) != src:
            raise ValueError("splice mismatch")
        if not changes:  # parsed equal but bytes differ (formatting only)
            return _hunks(dl, sl)
        return changes
    except (yaml.YAMLError, ValueError, UnicodeDecodeError, IndexError):
        return _hunks(dl, sl)


def apply(dest: bytes, src: bytes, changes: list[Change], accepted: set[int]) -> bytes:
    """Splice the accepted changes into `dest`, keeping all other bytes."""
    if len(accepted) == len(changes) and changes:
        edits = list(range(len(changes)))
    else:
        edits = sorted(accepted)
    dl, sl = split_lines(dest), split_lines(src)
    out = list(dl)
    for i in sorted(edits, key=lambda k: (changes[k].dest[0], changes[k].dest[1]), reverse=True):
        c = changes[i]
        new = sl[c.src[0]:c.src[1]]
        if new and out[c.dest[0]:c.dest[1]] == [] and c.dest[0] > 0 and not out[c.dest[0] - 1].endswith("\n"):
            out[c.dest[0] - 1] += "\r\n" if "\r\n" in "".join(dl) else "\n"
        out[c.dest[0]:c.dest[1]] = new
    return "".join(out).encode("utf-8")


# ------------------------------------------------------------------- display

class Style:
    def __init__(self, color: bool):
        self.on = color

    def _c(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.on else s

    red = lambda self, s: self._c("31", s)
    green = lambda self, s: self._c("32", s)
    cyan = lambda self, s: self._c("36", s)
    bold = lambda self, s: self._c("1", s)
    dim = lambda self, s: self._c("2", s)


MAX_SHOWN = 12


def _show(lines, prefix, paint, out):
    shown = lines[:MAX_SHOWN]
    for ln in shown:
        out(paint(f"    {prefix} {ln.rstrip(chr(13) + chr(10))}"))
    if len(lines) > MAX_SHOWN:
        out(paint(f"    {prefix} ... {len(lines) - MAX_SHOWN} more lines"))


def render(change: Change, dl, sl, names, st: Style, out=print, index=None, total=None):
    tag = f"[{index}/{total}] " if index else ""
    out(st.bold(f"  {tag}{change.path}") + (st.dim(f"   ({change.context})") if change.context else ""))
    old, new = dl[change.dest[0]:change.dest[1]], sl[change.src[0]:change.src[1]]
    if old:
        out(st.dim(f"    {names[0]}:"))
        _show(old, "-", st.red, out)
    if new:
        out(st.dim(f"    {names[1]}:"))
        _show(new, "+", st.green, out)
    if not old:
        out(st.dim(f"    (only in {names[1]})"))
    elif not new:
        out(st.dim(f"    (only in {names[0]})"))


class Session:
    """Asks about each change; remembers 'all' / 'none' answers across files."""

    def __init__(self, names=("radio/", "card"), color=None, ask=input, out=print):
        self.names = names
        self.st = Style(sys.stdout.isatty() if color is None else color)
        self.ask, self.out = ask, out
        self.auto: str | None = None

    def review(self, label: str, dest: bytes, src: bytes, changes: list[Change]) -> set[int]:
        dl, sl = split_lines(dest), split_lines(src)
        self.out("")
        self.out(self.st.cyan(f"{label}") + f"  {len(changes)} change{'s' * (len(changes) != 1)}"
                 + self.st.dim(f"   - {self.names[0]}   + {self.names[1]}"))
        accepted: set[int] = set()
        sticky = None  # 'y' / 'n' once the user chose "rest of this file"
        for i, c in enumerate(changes):
            render(c, dl, sl, self.names, self.st, self.out, i + 1, len(changes))
            if self.auto:
                take = self.auto == "A"
            elif sticky:
                take = sticky == "y"
            else:
                ans = self._prompt()
                if ans in ("A", "N"):
                    self.auto = ans
                elif ans in ("a", "r"):
                    sticky = "y" if ans == "a" else "n"
                take = ans in ("y", "a", "A")
            if take:
                accepted.add(i)
            self.out(self.st.green("    accepted") if take else self.st.red("    rejected"))
        return accepted

    def _prompt(self) -> str:
        q = (f"    take {self.names[1]} version? [y]es [n]o  [a]ll in file  [r]eject rest of file  "
             f"[A]ll files  [N]one of the rest  [q]uit: ")
        while True:
            try:
                ans = self.ask(q).strip()
            except EOFError:
                raise ReviewAbort()
            if ans in ("y", "n", "a", "r", "A", "N"):
                return ans
            if ans in ("q", "Q"):
                raise ReviewAbort()
            self.out("    please answer y, n, a, r, A, N or q")


def show_only(label, dest, src, changes, names, out=print, color=None):
    st = Style(sys.stdout.isatty() if color is None else color)
    dl, sl = split_lines(dest), split_lines(src)
    out(st.cyan(label) + f"  {len(changes)} change{'s' * (len(changes) != 1)}"
        + st.dim(f"   - {names[0]}   + {names[1]}"))
    for i, c in enumerate(changes):
        render(c, dl, sl, names, st, out, i + 1, len(changes))

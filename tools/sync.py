"""Three-way merge of the card (radio), radio/ (last sync) and build/ (computer).

A setting changed on one side only is taken from that side without asking. Only a
setting changed on both sides, differently, is a conflict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tools import review


class SyncError(Exception):
    pass


@dataclass
class FilePlan:
    rel: Path
    owner: str
    base: bytes | None
    card: bytes | None
    comp: bytes | None
    a: list = field(default_factory=list)      # base -> card: changed on the radio
    b: list = field(default_factory=list)      # base -> build: changed on the computer
    groups: list = field(default_factory=list)  # conflicting changes, as ([a idx], [b idx])
    same: set = field(default_factory=set)     # b changes identical to an a change
    whole: bytes | None = None                 # result when the whole file is new
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.owner}  ({self.rel.as_posix()})" if self.owner else self.rel.as_posix()

    @property
    def empty(self) -> bool:
        return not (self.a or self.b or self.whole is not None)


def _clash(x, y) -> bool:
    (xl, xh), (yl, yh) = x.dest, y.dest
    if xl == xh and yl == yh:
        return xl == yl
    return xl < yh and yl < xh


def _same_edit(x, xl, y, yl) -> bool:
    return x.dest == y.dest and xl[x.src[0]:x.src[1]] == yl[y.src[0]:y.src[1]]


def plan_file(rel: Path, base: bytes | None, card: bytes | None, comp: bytes | None) -> FilePlan:
    owner = review.file_label(rel.as_posix(), *(x for x in (card, base, comp) if x))
    p = FilePlan(rel, owner, base, card, comp)
    if card is None:
        p.note = "not on the card; left alone"
        return p
    if base is None:
        p.whole = card
        p.note = "new on the radio"
        return p
    p.a = review.diff(base, card, rel.name)
    p.b = review.diff(base, comp, rel.name) if comp is not None else []
    if not (review.significant(p.a) or review.significant(p.b)):
        p.a, p.b = [], []  # only checksum / timer noise
        return p
    cl, ml = review.split_lines(card), review.split_lines(comp or b"")
    p.same = {j for j, y in enumerate(p.b) for x in p.a if _same_edit(x, cl, y, ml)}
    pairs = [(i, j) for i, x in enumerate(p.a) for j, y in enumerate(p.b) if j not in p.same and _clash(x, y)]
    parent: dict = {}

    def find(n):
        while parent.setdefault(n, n) != n:
            n = parent[n]
        return n

    for i, j in pairs:
        parent[find(("a", i))] = find(("b", j))
    comps: dict = {}
    for n in list(parent):
        comps.setdefault(find(n), []).append(n)
    p.groups = [([n[1] for n in c if n[0] == "a"], [n[1] for n in c if n[0] == "b"]) for c in comps.values()]
    return p


def result(p: FilePlan, choices: list[str]) -> bytes:
    """Merged bytes; `choices[k]` is "radio" or "computer" for conflict group k."""
    if p.whole is not None:
        return p.whole
    drop_a, drop_b = set(), set(p.same)
    for (ai, bi), pick in zip(p.groups, choices):
        (drop_b if pick == "radio" else drop_a).update(bi if pick == "radio" else ai)
    cl, ml = review.split_lines(p.card), review.split_lines(p.comp or b"")
    edits = [(c.dest[0], c.dest[1], cl[c.src[0]:c.src[1]]) for i, c in enumerate(p.a) if i not in drop_a]
    edits += [(c.dest[0], c.dest[1], ml[c.src[0]:c.src[1]]) for j, c in enumerate(p.b) if j not in drop_b]
    out = splice(p.base, edits)
    try:
        yaml.safe_load(p.base)
    except yaml.YAMLError:
        return out
    try:
        yaml.safe_load(out)
    except yaml.YAMLError as exc:
        raise SyncError(f"{p.rel.as_posix()}: merged file is not valid YAML ({exc}); nothing written")
    return out


def splice(base: bytes, edits: list[tuple[int, int, list[str]]]) -> bytes:
    out = review.split_lines(base)
    eol = "\r\n" if b"\r\n" in base else "\n"
    for _, (lo, hi, new) in sorted(enumerate(edits), key=lambda x: (x[1][0], x[1][1], x[0]), reverse=True):
        if new and lo == hi and lo > 0 and not out[lo - 1].endswith("\n"):
            out[lo - 1] += eol
        out[lo:hi] = new
    return "".join(out).encode("utf-8")


def brief(c, old: list[str], new: list[str]) -> str:
    o, n = old[c.dest[0]:c.dest[1]], new[c.src[0]:c.src[1]]
    if not o:
        return "added"
    if not n:
        return "removed"
    if len(o) == 1 and len(n) == 1:
        val = lambda s: s.split(":", 1)[1].strip() if ":" in s else s.strip()
        return f"{val(o[0])} -> {val(n[0])}"
    return "changed"


def describe(p: FilePlan, chosen: list[str] | None = None) -> list[str]:
    """Lines for one file: what goes which way."""
    if p.note:
        return [f"  {p.note}"]
    bl, cl, ml = (review.split_lines(x or b"") for x in (p.base, p.card, p.comp))
    chosen = chosen or []
    drop_a, drop_b = set(), set(p.same)
    for (ai, bi), pick in zip(p.groups, chosen):
        (drop_b if pick == "radio" else drop_a).update(bi if pick == "radio" else ai)
    in_conflict_a = {i for ai, _ in p.groups for i in ai}
    in_conflict_b = {j for _, bi in p.groups for j in bi}
    lines = []
    for i, c in enumerate(p.a):
        if c.noise or i in drop_a or (i in in_conflict_a and not chosen):
            continue
        lines.append(f"  radio -> computer  {c.title or c.path}: {brief(c, bl, cl)}")
    for j, c in enumerate(p.b):
        if c.noise or j in drop_b or j in p.same or (j in in_conflict_b and not chosen):
            continue
        lines.append(f"  computer -> radio  {c.title or c.path}: {brief(c, bl, ml)}")
    if not chosen:
        for ai, bi in p.groups:
            names = ", ".join(sorted({(p.a[i].title or p.a[i].path) for i in ai}))
            lines.append(f"  CONFLICT           {names}")
    return lines


def ask_conflict(p: FilePlan, group, session, k: int, total: int) -> str:
    """Show both versions of a conflicting setting and ask which to keep."""
    ai, bi = group
    bl, cl, ml = (review.split_lines(x or b"") for x in (p.base, p.card, p.comp))
    st, out = session.st, session.out
    title = ", ".join(sorted({(p.a[i].title or p.a[i].path) for i in ai}))
    out("")
    out(st.bold(f"  [{k}/{total}] {p.owner + ': ' if p.owner else ''}{title}") + st.dim("   changed on both sides"))
    was = sorted({x for i in ai for x in bl[p.a[i].dest[0]:p.a[i].dest[1]]} | {x for j in bi for x in bl[p.b[j].dest[0]:p.b[j].dest[1]]})
    for label, lines, paint in (("was", was, st.dim),
                                ("radio", [x for i in ai for x in cl[p.a[i].src[0]:p.a[i].src[1]]], st.green),
                                ("computer", [x for j in bi for x in ml[p.b[j].src[0]:p.b[j].src[1]]], st.red)):
        out(st.dim(f"    {label}:"))
        review._show(lines or ["(removed)\n"], " ", paint, out)
    if session.prefer:
        return session.prefer
    q = "    keep [r]adio  [c]omputer  [R]adio for all  [C]omputer for all  [q]uit: "
    while True:
        try:
            ans = session.ask(q).strip()
        except EOFError:
            raise review.ReviewAbort()
        if ans in ("r", "c"):
            return "radio" if ans == "r" else "computer"
        if ans in ("R", "C"):
            session.prefer = "radio" if ans == "R" else "computer"
            return session.prefer
        if ans in ("q", "Q"):
            raise review.ReviewAbort()
        out("    please answer r, c, R, C or q")

"""Loader for maps/entries.bin + maps/entries.idx.json (built by tools/record_entries.py). Stdlib only.

A test is "sliceable" if it iterates fixture loader entries (tests.helpers.load_sql_fixture[_pair]s).
Per sliceable test we hold:
  base    lines executed with ZERO fixture entries (fn / top kinds, incl. import time)
  entries per fixture file and entry index i: the lines entry i alone executes beyond base
Kinds: "fn" (code in function bodies), "top" (module/class bodies), "any" (either).
"""
from __future__ import annotations

import json
import os
import zlib

_dir = None
_idx = {"tests": {}, "nonsliceable": {}}
_fh = None
_cache = {}
_EMPTY = frozenset()


def load(dir):
    global _dir, _idx, _fh, _cache
    _dir = os.fspath(dir)
    with open(os.path.join(_dir, "entries.idx.json")) as f:
        _idx = json.load(f)
    if _fh is not None:
        _fh.close()
    _fh = None
    _cache = {}


def _undelta(ds):
    out = []
    p = 0
    for d in ds:
        p += d
        out.append(p)
    return out


class _T:
    __slots__ = ("raw", "base", "files", "lines")

    def __init__(self, raw):
        self.raw = raw
        self.base = {}   # kind -> file -> frozenset(lines)
        for kind in ("fn", "top"):
            self.base[kind] = {f: frozenset(_undelta(v)) for f, v in raw["base"][kind].items()}
        self.files = raw["f"]
        self.lines = {}  # (fixture, kind, file) -> {line: tuple(entry idx)}

    def line_map(self, fixture, kind, file):
        key = (fixture, kind, file)
        m = self.lines.get(key)
        if m is None:
            m = {}
            enc = self.files[fixture].get(kind, {}).get(file)
            if enc:
                groups = [tuple(_undelta(s)) for s in enc["S"]]
                for ln, g in zip(_undelta(enc["L"]), enc["G"]):
                    m[ln] = groups[g]
            self.lines[key] = m
        return m


def _get(test_number):
    n = str(test_number)
    t = _cache.get(n)
    if t is None:
        ent = _idx["tests"].get(n)
        if ent is None:
            return None
        global _fh
        if _fh is None:
            _fh = open(os.path.join(_dir, "entries.bin"), "rb")
        _fh.seek(ent[0])
        t = _cache[n] = _T(json.loads(zlib.decompress(_fh.read(ent[1]))))
    return t


def _norm(file):
    f = file.replace("\\", "/")
    while f.startswith("./"):
        f = f[2:]
    return f


def sliceable(test_number):
    """{fixture_arg: n_entries} or None when the test cannot be sliced."""
    t = _get(test_number)
    if t is None:
        return None
    return {k: v["n"] for k, v in t.files.items()}


def loader(test_number, fixture):
    """'pairs' | 'fixtures' (which tests.helpers loader the test uses for that file)."""
    t = _get(test_number)
    return t.files[fixture]["l"] if t else None


def unused(test_number, fixture):
    """True if the test iterates the file (e.g. in setUpClass) but never executes any entry-dependent line."""
    t = _get(test_number)
    return bool(t and t.files.get(fixture, {}).get("u"))


def not_sliceable_reason(test_number):
    return _idx["nonsliceable"].get(str(test_number))


def in_base(test_number, file, line, kind="any"):
    t = _get(test_number)
    if t is None:
        return True
    file = _norm(file)
    kinds = ("fn", "top") if kind == "any" else (kind,)
    return any(line in t.base[k].get(file, _EMPTY) for k in kinds)


def needs_full(test_number, file, line, kind="any") -> bool:
    """True if the test must run unsliced for a change at (file, line): not sliceable, or the line is in base."""
    try:
        return in_base(test_number, file, line, kind)
    except Exception:
        return True


def entries(test_number, file, line, kind="any"):
    """{fixture_arg: {entry indices whose own coverage contains the line}} (only non-empty)."""
    t = _get(test_number)
    if t is None:
        return {}
    file = _norm(file)
    kinds = ("fn", "top") if kind == "any" else (kind,)
    out = {}
    for fx, fe in t.files.items():
        if fe.get("u"):
            continue
        s = set()
        for k in kinds:
            g = t.line_map(fx, k, file).get(line)
            if g:
                s.update(g)
        if s:
            out[fx] = s
    return out


def entry_cpu(test_number):
    t = _get(test_number)
    if t is None:
        return {}
    return {k: list(v["cpu"]) for k, v in t.files.items() if "cpu" in v}


def base_cpu(test_number):
    t = _get(test_number)
    return t.raw["bcpu"] if t else 0.0


def fixture_files(test_number):
    t = _get(test_number)
    return list(t.files) if t else []


def full_cpu(test_number):
    """Unmonitored CPU seconds of the full test in a fresh forked child (inline pools)."""
    t = _get(test_number)
    return t.raw.get("fcpu", 0.0) if t else 0.0


def est_cpu(test_number, selection):
    """Estimated CPU seconds of a slice. selection: {fixture_arg: iterable of entry indices}.
    entry_cpu values contain a cold-start overhead per entry, so they are only used as weights:
    base + (full - base) * (selected weight / total weight)."""
    t = _get(test_number)
    if t is None:
        return 0.0
    base, full = t.raw["bcpu"], t.raw.get("fcpu", 0.0)
    tot = sel = 0.0
    for fx, fe in t.files.items():
        if fe.get("u") or "cpu" not in fe:
            continue
        w = fe["cpu"]
        tot += sum(w) + 1e-9 * len(w)
        s = selection.get(fx, ())
        sel += sum(w[i] for i in s if i < len(w)) + 1e-9 * len(s)
    if tot <= 0:
        return full
    return base + max(0.0, full - base) * min(1.0, sel / tot)

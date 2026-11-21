"""Lazy loader for maps/ (built by tools/buildmaps.py). Stdlib only."""
from __future__ import annotations

import array
import json
import os
import sys
import zlib

_dir = None
_index = {}
_fh = None
_cache = {}      # file -> _FileCov
_meta = None
_reads = None
_EMPTY = frozenset()


class _FileCov:
    __slots__ = ("lines", "fi", "ti", "data", "offs", "sets", "pos", "memo")

    def __init__(self, blob):
        raw = zlib.decompress(blob)
        nsets, nlines = array.array("I", raw[:8])
        p = 8
        offs = array.array("I")
        offs.frombytes(raw[p:p + 4 * (nsets + 1)])
        p += 4 * (nsets + 1)
        arrs = []
        for _ in range(3):
            a = array.array("I")
            a.frombytes(raw[p:p + 4 * nlines])
            p += 4 * nlines
            arrs.append(a)
        if sys.byteorder != "little":
            offs.byteswap()
            for a in arrs:
                a.byteswap()
        self.offs = offs
        self.lines = arrs[0]
        self.fi = arrs[1]
        self.ti = arrs[2]
        self.data = raw[p:]
        self.sets = {}
        self.pos = None
        self.memo = {}

    def line_pos(self, line):
        if self.pos is None:
            self.pos = {ln: i for i, ln in enumerate(self.lines)}
        return self.pos.get(line)

    def get_set(self, i):
        if i == 0:
            return _EMPTY
        s = self.sets.get(i)
        if s is None:
            d = self.data
            p = self.offs[i]
            cnt = 0
            shift = 0
            while True:
                b = d[p]
                p += 1
                cnt |= (b & 0x7F) << shift
                if b < 0x80:
                    break
                shift += 7
            vals = []
            prev = 0
            for _ in range(cnt):
                v = 0
                shift = 0
                while True:
                    b = d[p]
                    p += 1
                    v |= (b & 0x7F) << shift
                    if b < 0x80:
                        break
                    shift += 7
                prev += v
                vals.append(prev)
            s = self.sets[i] = frozenset(vals)
        return s


def load(dir):
    global _dir, _index, _fh, _cache, _meta, _reads
    _dir = os.fspath(dir)
    with open(os.path.join(_dir, "index.json")) as f:
        _index = json.load(f)
    if _fh is not None:
        _fh.close()
    _fh = None
    _cache = {}
    _meta = None
    _reads = None


def _norm(file):
    f = file.replace("\\", "/")
    while f.startswith("./"):
        f = f[2:]
    return f


def _cov(file):
    global _fh
    c = _cache.get(file)
    if c is None:
        ent = _index.get(file)
        if ent is None:
            return None
        if _fh is None:
            _fh = open(os.path.join(_dir, "cov.bin"), "rb")
        _fh.seek(ent[0])
        c = _cache[file] = _FileCov(_fh.read(ent[1]))
    return c


def known_file(file) -> bool:
    return _norm(file) in _index


def lines(file) -> set:
    c = _cov(_norm(file))
    return set(c.lines) if c else set()


def tests_for(file, line, kind="any"):
    c = _cov(_norm(file))
    if c is None:
        return _EMPTY
    i = c.line_pos(line)
    if i is None:
        return _EMPTY
    if kind == "fn":
        return c.get_set(c.fi[i])
    if kind == "top":
        return c.get_set(c.ti[i])
    key = (c.fi[i], c.ti[i])
    r = c.memo.get(key)
    if r is None:
        a, b = c.get_set(key[0]), c.get_set(key[1])
        r = c.memo[key] = (a | b) if (a and b) else (a or b)
    return r


def tests_for_file(file, kind="any"):
    c = _cov(_norm(file))
    if c is None:
        return _EMPTY
    ids = set()
    if kind in ("any", "fn"):
        ids.update(c.fi)
    if kind in ("any", "top"):
        ids.update(c.ti)
    out = set()
    for i in ids:
        out |= c.get_set(i)
    return frozenset(out)


def _load_meta():
    global _meta
    if _meta is None:
        with open(os.path.join(_dir, "meta.json")) as f:
            _meta = json.load(f)
    return _meta


def tests_reading(path):
    global _reads
    if _reads is None:
        with open(os.path.join(_dir, "reads.json.z"), "rb") as f:
            _reads = json.loads(zlib.decompress(f.read()))
    return frozenset(_reads.get(_norm(path), ()))


def test_ids():
    return list(_load_meta()["tests"])


def isolated_failures():
    m = _load_meta()
    return set(m["failed_isolated"]) | set(m["not_recorded"])


def test_cpu():
    return list(_load_meta()["cpu"])

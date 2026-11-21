"""Fixture-entry slicing: run only some entries of a fixture-driven test. Stdlib only.

Mechanism (install): the two loader functions in ``tests.helpers`` are generator functions whose
``__code__`` we replace IN PLACE with a filtering generator (compiled inside ``tests.helpers``'
globals).  Every reference to the function object -- ``from tests.helpers import load_sql_fixture_pairs``
in any test module, already imported or imported later -- therefore sees the filter, with no
import hooks and no rebinding.  The original behavior is kept as a plain copy of the original code
object under ``_br_orig_*`` in the ``tests.helpers`` namespace.

Filter semantics: ``set_filter({"optimizer/simplify.sql": {3, 7}})`` -> the loader for that exact
filename argument yields only entries with 0-based yield index 3 and 7; files without a filter
yield everything.  The loader still parses the whole file (errors in unselected entries are
still raised, which is conservative).
"""
from __future__ import annotations

import collections
import json
import sys
import types

_STATE = {"filter": {}, "default": None, "seen": {}, "complete": {}, "phase": "", "installed": False}
_ORIG = {}


class SliceError(Exception):
    """Anything that makes slicing unsafe: caller must run the full test."""


_SRC = '''
def _br_pairs(filename):
    st = _br_state
    allowed = st["filter"].get(filename, st["default"])
    n = 0
    done = False
    try:
        for item in _br_orig_pairs(filename):
            if allowed is None or n in allowed:
                yield item
            n += 1
        done = True
    finally:
        seen = st["seen"]
        key = (st["phase"], filename, "pairs")
        seen[key] = max(seen.get(key, 0), n)
        st["complete"][key] = done
'''
_SRC2 = (_SRC.replace("_br_pairs", "_br_fixtures").replace("_br_orig_pairs", "_br_orig_fixtures")
         .replace('"pairs")', '"fixtures")'))


def install():
    """Wrap tests.helpers loaders (idempotent). Call after harness.setup(root)."""
    try:
        import tests.helpers as h
        if _STATE["installed"] and getattr(h, "_br_state", None) is _STATE:
            return
        g = h.__dict__
        for name, src, fname in (("load_sql_fixture_pairs", _SRC, "_br_pairs"),
                                 ("load_sql_fixtures", _SRC2, "_br_fixtures")):
            fn = g[name]
            tag = "pairs" if "pairs" in fname else "fixtures"
            orig_copy = types.FunctionType(fn.__code__, g, name + "_orig")
            g["_br_orig_" + tag] = orig_copy
            g["_br_state"] = _STATE
            ns = {}
            exec(compile(src, "<br.slices>", "exec"), g, ns)
            wrapper = ns[fname]
            if not (fn.__code__.co_flags & 0x20) or not (wrapper.__code__.co_flags & 0x20):
                raise SliceError("loader is not a generator function")
            if fn.__code__.co_argcount != 1 or fn.__defaults__:
                raise SliceError("unexpected loader signature")
            fn.__code__ = wrapper.__code__
            _ORIG[name] = orig_copy
        _STATE["installed"] = True
    except SliceError:
        raise
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        raise SliceError(f"install failed: {e!r}") from e


def set_filter(mapping, default_empty=False):
    """mapping: {loader filename arg: allowed 0-based entry indices}.  default_empty=True makes every
    file NOT in the mapping yield zero entries (recorder use); default is unfiltered."""
    _STATE["filter"] = {k: frozenset(int(i) for i in v) for k, v in mapping.items()}
    _STATE["default"] = frozenset() if default_empty else None


def clear():
    _STATE["filter"] = {}
    _STATE["default"] = None


def set_phase(name):
    """Tag subsequent loader calls (used by the recorder: 'class' vs 'test')."""
    _STATE["phase"] = name


def reset_seen():
    _STATE["seen"] = {}
    _STATE["complete"] = {}


def seen():
    """{(phase, filename, loader): (n_yielded_by_original, completed)}"""
    return {k: (v, _STATE["complete"].get(k, False)) for k, v in _STATE["seen"].items()}


def fixture_arg(path):
    """'tests/fixtures/optimizer/simplify.sql' -> 'optimizer/simplify.sql' (else None)."""
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    pre = "tests/fixtures/"
    if not p.startswith(pre) or not p.endswith(".sql"):
        return None
    rest = p[len(pre):]
    return rest or None


# ----------------------------------------------------------------- faithful parsers (tests/helpers.py)

def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    sql = "\n".join(sql_lines[i:])
    return sql, meta


def parse_fixtures(text):
    out = []
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


def parse_pairs(text):
    out = []
    statements = _filter_comments(text).split(";")
    size = len(statements)
    for i in range(0, size, 2):
        if i + 1 < size:
            sql = statements[i].strip()
            sql, meta = _extract_meta(sql)
            expected = statements[i + 1].strip()
            out.append((meta, sql, expected))
    return out


def _key(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=True)


def changed_entries(loader_name, old_text, new_text):
    """Indices (in the NEW file) of entries not present in the old file (multiset compare).
    None if slicing is unsafe (parse trouble, or nothing new)."""
    try:
        parse = {"load_sql_fixture_pairs": parse_pairs, "load_sql_fixtures": parse_fixtures,
                 "pairs": parse_pairs, "fixtures": parse_fixtures}[loader_name]
        old = parse(old_text)
        new = parse(new_text)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return None
    pool = collections.Counter(_key(e) for e in old)
    out = set()
    for i, e in enumerate(new):
        k = _key(e)
        if pool.get(k, 0) > 0:
            pool[k] -= 1
        else:
            out.add(i)
    return out or None

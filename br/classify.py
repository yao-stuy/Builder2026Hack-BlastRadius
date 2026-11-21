"""Classify a per-file patch: which OLD lines must a test execute to be affected?

``classify(path, old_text, new_text, fp) -> Impact``; never raises (any internal
error yields ``kind="whole_file"``).  Conservative by construction: when in doubt
widen the line set or give up with ``whole_file``.

Performance notes (every CPU second counts at scoring time)
-----------------------------------------------------------
* The pristine file's parse tree and index are cached per path (``_OLD``).
* A *fast path* (``_fast``) never parses/compiles/tokenizes the whole NEW file.  The
  hunks are grouped into *regions* (consecutive sibling statements of a module or
  class body, or one whole top-level/class-level function) plus "commentary runs"
  (only blank/comment-only lines changed).  Everything outside the regions is
  verified to be textually identical, regions are re-parsed and compiled on their
  own, and the usual line-mapping logic then runs against a small synthetic "view" of
  the new tree.  Whenever a precondition of the fast path cannot be established it
  raises ``_Fallback`` and the original whole-file algorithm (``_classify_full``,
  kept bit-for-bit equivalent to the old implementation) answers instead.
* Soundness of the fast path rests on the tokenizer/parser being deterministic and
  left-to-right: text before a region is identical, a region starts and ends on a
  statement boundary of the old file, so the new token stream is
  ``prefix + tokens(new region) + suffix`` and parses/compiles iff the region does
  (see ``_verify_region`` for the scope-related guards).  The pristine file is assumed
  to compile (it parses, which we check; a file that parses but fails ``compile`` is
  not a realistic pristine input).
"""

from __future__ import annotations

import ast
import copy
import io
import re
import tokenize
import warnings
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from . import diffparse

_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFS_CLS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_SKIP_TOK = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
}


@dataclass
class Impact:
    kind: str  # noop | lines | whole_file | syntax_error | nonpython
    old_lines: set[int] = field(default_factory=set)
    scopes: list[str] = field(default_factory=list)
    level: str | None = None  # fn | top | mixed (kind == "lines" only)
    lineno_shift: bool = False
    comment_text: str = ""
    structural: bool = False
    insert_points: list[tuple[int, int]] = field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- helpers


class _Give(Exception):
    """Cannot classify safely -> whole_file."""


class _Fallback(Exception):
    """Fast path precondition failed -> use the whole-file algorithm."""


def _code_lines(text: str) -> set[int]:
    out: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in _SKIP_TOK:
                continue
            out.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        for i, ln in enumerate(text.split("\n"), 1):
            s = ln.strip()
            if s and not s.startswith("#"):
                out.add(i)
    return out


def _start(n: ast.AST) -> int:
    if isinstance(n, _DEFS_CLS):
        return min([n.lineno] + [d.lineno for d in n.decorator_list])
    return n.lineno  # type: ignore[attr-defined]


def _end(n: ast.AST) -> int:
    return n.end_lineno  # type: ignore[attr-defined]


def _child_groups(n: ast.AST) -> list[list[ast.stmt]]:
    groups: list[list[ast.stmt]] = []
    for f in ("body", "orelse", "finalbody"):
        v = getattr(n, f, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            groups.append(v)
    for h in getattr(n, "handlers", None) or []:
        groups.append(h.body)
    for c in getattr(n, "cases", None) or []:
        groups.append(c.body)
    return groups


_CHILD_FIELDS = {"body", "orelse", "finalbody", "handlers", "cases"}


def _own_nodes(n: ast.AST):
    """Expression nodes belonging to the statement itself (not nested statements)."""
    for name, val in ast.iter_fields(n):
        if name in _CHILD_FIELDS and (
            name != "body" or (isinstance(val, list) and val and isinstance(val[0], ast.stmt))
        ):
            continue
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if isinstance(v, ast.AST):
                yield from ast.walk(v)


@dataclass
class _Unit:
    key: tuple
    lines: frozenset
    node: ast.AST  # statement (or Try for handlers) whose position decides scope/level
    kind: str  # stmt | hdr | handler | gap
    self_scope: bool  # node itself is a def/class whose header this unit is
    chain: list


def _qual_from_chain(chain: list, k: int) -> str:
    """Qualified name of def/class ``chain[k]`` given its statement ancestors."""
    prefix = ""
    for m in chain[:k]:
        if isinstance(m, _DEFS_CLS):
            prefix += m.name + (".<locals>." if isinstance(m, _DEFS) else ".")
    return prefix + chain[k].name  # type: ignore[attr-defined]


class _Index:
    """Positional index of a module.

    ``code`` is the set of lines carrying tokens (new files); when ``None`` (old files)
    the "is this a code line" question is only ever asked about lines that lie in the
    gaps between statements, where a blank/``#`` test on the physical line is exact.
    """

    def __init__(self, text: str | None, tree: ast.Module, code: set[int] | None = None,
                 lines: list[str] | None = None):
        self.lines = lines if lines is not None else text.split("\n")  # type: ignore[union-attr]
        self.tree = tree
        self.code = code
        self._gb: dict[int, tuple] = {}
        self._walked = False
        self.parent: dict[int, ast.AST] = {}
        self.qual: dict[int, str] = {}
        self.by_qual_: dict[str, list[ast.AST]] = {}

    # -- lazy whole-tree walk (needed only for insertions)
    def ensure_walk(self) -> None:
        if not self._walked:
            self._walked = True
            self._walk(self.tree, self.tree, "")

    @property
    def by_qual(self) -> dict[str, list[ast.AST]]:
        self.ensure_walk()
        return self.by_qual_

    def _walk(self, node: ast.AST, parent: ast.AST, prefix: str) -> None:
        self._walk_groups(([node.body] if isinstance(node, ast.Module) else _child_groups(node)),
                          node, prefix)

    def _walk_groups(self, groups, node: ast.AST, prefix: str) -> None:
        for group in groups:
            for s in group:
                self.parent[id(s)] = node
                child_prefix = prefix
                if isinstance(s, _DEFS_CLS):
                    q = prefix + s.name
                    self.qual[id(s)] = q
                    self.by_qual_.setdefault(q, []).append(s)
                    child_prefix = q + (".<locals>." if isinstance(s, _DEFS) else ".")
                self._walk(s, node, child_prefix)

    # -- positions
    def line_text(self, n: int) -> str:
        return self.lines[n - 1] if 1 <= n <= len(self.lines) else ""

    def is_code(self, L: int) -> bool:
        if self.code is not None:
            return L in self.code
        s = self.line_text(L).strip()
        return bool(s) and not s.startswith("#")

    def _trim_hdr(self, first_line: int, hdr_end: int) -> int:
        while hdr_end > first_line:
            s = self.line_text(hdr_end).strip()
            if s == "" or s.startswith("#"):
                hdr_end -= 1
            else:
                break
        return hdr_end

    def hdr_lines(self, n: ast.AST) -> range:
        st = _start(n)
        firsts = []
        if isinstance(getattr(n, "body", None), list) and n.body and isinstance(n.body[0], ast.stmt):
            firsts.append(_start(n.body[0]))
        for c in getattr(n, "cases", None) or []:
            firsts.append(c.pattern.lineno)
        if not firsts:
            return range(st, _end(n) + 1)
        f = min(firsts)
        end = f if f == _node_header_line(n) else f - 1
        if f <= st:
            end = st
        return range(st, self._trim_hdr(st, max(end, st)) + 1)

    def handler_hdr(self, h: ast.ExceptHandler) -> range:
        f = _start(h.body[0])
        end = f if f == h.lineno else f - 1
        return range(h.lineno, self._trim_hdr(h.lineno, max(end, h.lineno)) + 1)

    # -- unit properties (chain based: no parent map needed)
    @staticmethod
    def is_fn_ctx(u: _Unit) -> bool:
        """Is the unit's statement located inside some function body (strict ancestors)?"""
        for m in u.chain[:-1]:
            if isinstance(m, _DEFS):
                return True
        return False

    @staticmethod
    def scope_of(u: _Unit) -> str:
        chain = u.chain
        k = len(chain) - 1
        if not u.self_scope:
            k -= 1
        while k >= 0 and not isinstance(chain[k], _DEFS_CLS):
            k -= 1
        if k < 0:
            return "<module>"
        return _qual_from_chain(chain, k)

    def is_docstring(self, u: _Unit) -> ast.AST | None:
        n = u.node
        if u.kind != "stmt" or not isinstance(n, ast.Expr):
            return None
        v = n.value
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
            return None
        p = u.chain[-2] if len(u.chain) > 1 else self.tree
        if isinstance(p, (ast.Module, *_DEFS, ast.ClassDef)) and p.body and p.body[0] is n:
            return p
        return None

    # -- sibling lookup
    def group_bounds(self, g: list) -> tuple[list[int], list[int]]:
        b = self._gb.get(id(g))
        if b is None or b[2] is not g:
            b = ([_start(s) for s in g], [_end(s) for s in g], g)
            self._gb[id(g)] = b
        return b[0], b[1]

    def _cover(self, g: list, L: int):
        starts, ends = self.group_bounds(g)
        i = bisect_left(starts, L)
        if i > 0 and L <= ends[i - 1]:  # starts[i-1] < L
            return g[i - 1]
        if i < len(g) and starts[i] == L:
            return g[i]
        return None

    def find_unit(self, L: int) -> _Unit | None:
        groups: list[list[ast.stmt]] = [self.tree.body]
        cur: ast.AST | None = None
        chain: list[ast.AST] = []
        while True:
            found = None
            for g in groups:
                found = self._cover(g, L)
                if found is not None:
                    break
            if found is None:
                break
            cur = found
            chain.append(cur)
            groups = _child_groups(cur)
            if not groups and not _is_compound(cur):
                return _Unit((id(cur), "stmt"), frozenset(range(_start(cur), _end(cur) + 1)), cur,
                             "stmt", False, chain)
        if cur is None:
            return None
        hdr = self.hdr_lines(cur)
        selfscope = isinstance(cur, _DEFS_CLS)
        if L in hdr:
            return _Unit((id(cur), "hdr"), frozenset(hdr), cur, "hdr", selfscope, chain)
        for h in getattr(cur, "handlers", None) or []:
            if h.lineno <= L <= _end(h):
                hh = self.handler_hdr(h)
                if L in hh:
                    return _Unit((id(h), "handler"), frozenset(hh), cur, "handler", False, chain)
                if self.is_code(L):
                    return _Unit((id(h), "gap"), frozenset(hh) | {L}, cur, "gap", False, chain)
                return None
        if self.is_code(L):
            return _Unit((id(cur), "gap", L), frozenset(hdr) | {L}, cur, "gap", selfscope, chain)
        return None

    def multiline_string_at(self, x: int, y: int) -> bool:
        """Is there a multi-line string literal with start <= y and end >= x touching the
        statement that (innermost) covers line x?  Used with x == y for a single line and
        with (b, b + 1) for the boundary between two lines."""
        for L in {x, y}:
            u = self._innermost_stmt(L)
            if u is None:
                continue
            nodes = _own_nodes(u) if _is_compound(u) else ast.walk(u)
            for n in nodes:
                if isinstance(n, (ast.Constant, ast.JoinedStr)) and (
                    isinstance(n, ast.JoinedStr) or isinstance(n.value, (str, bytes))
                ):
                    if n.end_lineno > n.lineno and n.lineno <= y and n.end_lineno >= x:  # type: ignore[operator]
                        return True
        return False

    def _innermost_stmt(self, L: int):
        groups = [self.tree.body]
        cur = None
        while True:
            found = None
            for g in groups:
                found = self._cover(g, L)
                if found is not None:
                    break
            if found is None:
                return cur
            cur = found
            groups = _child_groups(cur)


def _is_compound(n: ast.AST) -> bool:
    return any(
        isinstance(getattr(n, f, None), list) and getattr(n, f)
        and isinstance(getattr(n, f)[0], (ast.stmt, ast.ExceptHandler, ast.match_case))
        for f in ("body", "orelse", "finalbody", "handlers", "cases")
    )


def _node_header_line(n: ast.AST) -> int:
    return n.lineno  # type: ignore[attr-defined]


def _future_set(tree: ast.Module) -> list[str]:
    out = []
    for s in tree.body:
        if isinstance(s, ast.ImportFrom) and s.module == "__future__":
            out.append(ast.dump(s))
    return out


# --------------------------------------------------------------------------- old-file cache


class _OldInfo:
    __slots__ = ("text", "tree", "idx")

    def __init__(self, text: str, tree: ast.Module):
        self.text = text
        self.tree = tree
        self.idx = _Index(text, tree)


_OLD: dict[str, _OldInfo] = {}
_OLD_BUDGET = 2_500_000  # chars of cached source (parse trees are ~100x larger)


def _clear_cache() -> None:
    _OLD.clear()


def _old_info(path: str, old_text: str) -> _OldInfo:
    info = _OLD.pop(path, None)
    if info is not None and (info.text is old_text or info.text == old_text):
        _OLD[path] = info  # most recently used goes last
        return info
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(old_text)
    info = _OldInfo(old_text, tree)
    _OLD[path] = info
    total = 0
    for k in reversed(list(_OLD)):
        total += len(_OLD[k].text)
        if total > _OLD_BUDGET and k != path:
            del _OLD[k]
    return info


# --------------------------------------------------------------------------- runs


@dataclass
class _Run:
    removed: list[tuple[int, str]]
    added: list[tuple[int, str]]
    before: int
    after: int


def _runs(fp: diffparse.FilePatch) -> list[_Run]:
    runs: list[_Run] = []
    for h in fp.hunks:
        last_old = h.old_start - 1 if h.old_count > 0 else h.old_start
        cur: _Run | None = None
        for ln in h.lines:
            if ln.tag == " ":
                if cur is not None:
                    cur.after = last_old + 1
                    runs.append(cur)
                    cur = None
                last_old = ln.old_no  # type: ignore[assignment]
                continue
            if cur is None:
                cur = _Run([], [], last_old, last_old + 1)
            if ln.tag == "-":
                cur.removed.append((ln.old_no, ln.text))  # type: ignore[arg-type]
                last_old = ln.old_no  # type: ignore[assignment]
            else:
                cur.added.append((ln.new_no, ln.text))  # type: ignore[arg-type]
        if cur is not None:
            cur.after = last_old + 1
            runs.append(cur)
    return runs


def _byte_col(idx: _Index, line: int, col: int) -> int:
    return len(idx.line_text(line)[:col].encode("utf-8", "surrogateescape"))


def _run_old_span(idx: _Index, r: _Run) -> tuple[tuple[int, int], tuple[int, int]]:
    """Changed character span of the run in OLD coordinates as ((line,bytecol),(line,bytecol))."""
    if not r.removed:
        p = (r.after, 0)
        return p, p
    bo = "\n".join(t for _, t in r.removed)
    bn = "\n".join(t for _, t in r.added)
    p = 0
    m = min(len(bo), len(bn))
    while p < m and bo[p] == bn[p]:
        p += 1
    s = 0
    while s < m - p and bo[len(bo) - 1 - s] == bn[len(bn) - 1 - s]:
        s += 1
    first = r.removed[0][0]

    def pos(off: int) -> tuple[int, int]:
        ln = first + bo.count("\n", 0, off)
        col = off - (bo.rfind("\n", 0, off) + 1)
        return ln, _byte_col(idx, ln, col)

    return pos(p), pos(len(bo) - s)


# --------------------------------------------------------------------------- main


def classify(path: str, old_text: str, new_text: str, fp: diffparse.FilePatch) -> Impact:
    try:
        return _classify(path, old_text, new_text, fp)
    except _Give as g:
        return Impact("whole_file", reason=str(g), structural=_structural(fp))
    except BaseException as e:  # noqa: BLE001 - must never raise
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return Impact("whole_file", reason=f"internal error: {e!r}", structural=_structural(fp))


def _structural(fp: diffparse.FilePatch) -> bool:
    try:
        return fp.status != "modify"
    except Exception:  # noqa: BLE001
        return False


def _has_lone_cr(text: str) -> bool:
    return "\r" in text and re.search(r"\r(?!\n)", text) is not None


def _classify(path: str, old_text: str, new_text: str, fp: diffparse.FilePatch) -> Impact:
    if not path.endswith(".py"):
        return Impact(
            "nonpython",
            old_lines=set(diffparse.removed_old_lines(fp)),
            insert_points=diffparse.insert_points(fp),
            structural=_structural(fp),
            reason="not a .py file",
        )
    if fp.binary:
        raise _Give("binary patch on a .py path")
    if fp.status != "modify":
        raise _Give(f"python file {fp.status}")
    if old_text is None or new_text is None:
        raise _Give("missing text")
    if _has_lone_cr(old_text) or _has_lone_cr(new_text):
        raise _Give("lone CR line endings")

    runs = _runs(fp)
    ipts = diffparse.insert_points(fp)
    added_text = "\n".join(t.strip() for r in runs for _, t in r.added)
    shift = any(len(r.removed) != len(r.added) for r in runs)

    try:
        res = _fast(path, old_text, new_text, fp, runs, ipts, added_text, shift)
    except _Fallback:
        res = None
    except _Give:
        raise
    except Exception:  # noqa: BLE001 - anything unexpected: use the exact algorithm
        res = None
    if res is not None:
        return res
    return _classify_full(path, old_text, new_text, fp, runs, ipts, added_text, shift)


# ---- the exact whole-file algorithm


def _classify_full(path, old_text, new_text, fp, runs, ipts, added_text, shift) -> Impact:
    # new source must compile
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(new_text, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError) as e:
        return Impact("syntax_error", reason=repr(e))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            old_info = _old_info(path, old_text)
            new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise _Give("old file does not parse") from None
    old_tree = old_info.tree

    # PEP 263 cookie: first two lines changed or shifted
    o2 = old_text.split("\n", 2)[:2]
    n2 = new_text.split("\n", 2)[:2]
    if o2 != n2 and any(_CODING_RE.match(x) for x in o2 + n2):
        raise _Give("encoding declaration touched")
    for r in runs:
        for no, t in r.removed + r.added:
            if no <= 2 and _CODING_RE.match(t):
                raise _Give("encoding declaration touched")

    if ast.dump(old_tree) == ast.dump(new_tree):
        pos_shift = False
        for a, b in zip(ast.walk(old_tree), ast.walk(new_tree)):
            if getattr(a, "lineno", None) != getattr(b, "lineno", None) or getattr(
                a, "end_lineno", None
            ) != getattr(b, "end_lineno", None):
                pos_shift = True
                break
        return Impact("noop", lineno_shift=pos_shift,
                      comment_text=added_text, insert_points=ipts, reason="identical AST")

    if _future_set(old_tree) != _future_set(new_tree):
        raise _Give("__future__ imports changed")

    old = old_info.idx
    new = _Index(new_text, new_tree, code=_code_lines(new_text))
    new.ensure_walk()
    return _lines_impact(old, new, runs, ipts, added_text, shift)


# ---- shared line-mapping logic


def _lines_impact(old: _Index, new, runs: list[_Run], ipts, added_text: str, shift: bool) -> Impact:
    lines: set[int] = set()
    scopes: set[str] = set()
    levels: list[str] = []

    def add_unit(u: _Unit, span: tuple[tuple[int, int], tuple[int, int]]) -> None:
        doc = old.is_docstring(u)
        if doc is not None:
            if isinstance(doc, ast.Module):
                raise _Give("module docstring changed")
            lines.update(old.hdr_lines(doc))
        scopes.add(old.scope_of(u))
        lvl = "fn" if old.is_fn_ctx(u) else "top"
        narrowed = None
        if lvl == "top" and u.kind in ("stmt", "hdr"):
            for n in _own_nodes(u.node):
                if isinstance(n, ast.Lambda):
                    b = n.body
                    lo = (b.lineno, b.col_offset)
                    hi = (b.end_lineno, b.end_col_offset)
                    if lo <= span[0] and span[1] <= hi:
                        # change lies wholly inside a lambda body: only tests that
                        # call the lambda can see it (innermost such lambda wins)
                        cand = range(n.lineno, n.end_lineno + 1)
                        if narrowed is None or len(cand) <= len(narrowed):
                            narrowed = cand
                        lvl = "fn"
        lines.update(narrowed if narrowed is not None else u.lines)
        levels.append(lvl)

    for r in runs:
        span = _run_old_span(old, r)
        old_units: dict[tuple, _Unit] = {}
        for no, _ in r.removed:
            u = old.find_unit(no)
            if u is not None:
                old_units[u.key] = u
        for u in old_units.values():
            add_unit(u, span)
        added_code = [no for no, _ in r.added if no in new.code]
        if not added_code:
            continue
        new_keys = set()
        for no in added_code:
            nu = new.find_unit(no)
            if nu is None:
                raise _Give("added code line outside any statement")
            new_keys.add(nu.key)
        if old_units and len(new_keys) <= len(old_units):
            continue  # in-place modification of existing statements
        if not r.removed:
            b, a = r.before, r.after
            if 1 <= b and a <= len(old.lines):
                ub, ua = old.find_unit(b), old.find_unit(a)
                if ub is not None and ua is not None and ub.key == ua.key and ub.kind in ("stmt", "hdr"):
                    add_unit(ub, span)
                    continue
        # genuine insertion: map through the NEW tree to enclosing old function/class
        for no in added_code:
            nu = new.find_unit(no)
            assert nu is not None
            containers = []
            for n in reversed(nu.chain):
                if isinstance(n, _DEFS_CLS) and _start(n.body[0]) <= no <= _end(n):
                    containers.append(n)
            if not containers:
                raise _Give("module-level insertion")
            done = False
            for ci, n in enumerate(containers):
                any_fn = any(isinstance(m, _DEFS) for m in containers[ci:])
                cps = old.by_qual.get(new.qual[id(n)])
                if not cps:
                    continue
                for cp in cps:
                    if isinstance(cp, _DEFS):
                        lines.update(range(_start(cp.body[0]), _end(cp) + 1))
                    else:
                        lines.update(old.hdr_lines(cp))
                    scopes.add(old.qual[id(cp)])
                levels.append("fn" if any_fn else "top")
                done = True
                break
            if not done:
                raise _Give("module-level insertion")

    if not lines:
        raise _Give("AST differs but no executable lines mapped")
    lines = {x for x in lines if 1 <= x <= len(old.lines)}
    if not lines:
        raise _Give("mapped lines out of range")
    lv = set(levels)
    level = "fn" if lv == {"fn"} else "top" if lv == {"top"} else "mixed"
    return Impact("lines", old_lines=lines, scopes=sorted(scopes), level=level,
                  lineno_shift=shift, comment_text=added_text, insert_points=ipts)


# --------------------------------------------------------------------------- fast path


def _blank_or_comment(t: str) -> bool:
    s = t.strip()
    return s == "" or s[0] == "#"


@dataclass
class _Region:
    chain: list  # enclosing ClassDef nodes (outermost first)
    group: list  # sibling list the region is a slice of
    i: int
    j: int
    s: int  # old first line
    e: int  # old last line
    ns: int = 0  # new first line
    ne: int = 0  # new last line
    delta: int = 0
    runs: list = field(default_factory=list)
    new_nodes: list = field(default_factory=list)


def _touched(r: _Run) -> tuple[int, int]:
    """Old lines a run touches; an insertion touches the two lines around its boundary."""
    if r.removed:
        return r.removed[0][0], r.removed[-1][0]
    return r.before, r.before + 1


def _commentary_ok(old: _Index, r: _Run) -> bool:
    """Run changes only blank/comment-only physical lines that are outside any string
    and not adjacent to a backslash continuation: token stream (minus COMMENT/NL) is
    unchanged."""
    for _, t in r.removed:
        if not _blank_or_comment(t):
            return False
    for _, t in r.added:
        if not _blank_or_comment(t):
            return False
    n = len(old.lines)
    if r.removed:
        lo, hi = r.removed[0][0], r.removed[-1][0]
        if hi - lo + 1 != len(r.removed):
            return False
        prev = lo - 1
    else:
        lo, hi = r.before, r.before + 1
        prev = r.before
    if prev >= 1 and old.lines[prev - 1].rstrip().endswith("\\"):
        return False
    if r.removed:
        if old.multiline_string_at(lo, hi):
            return False
    else:
        if 1 <= lo and hi <= n and old.multiline_string_at(lo, hi):
            return False
    return True


def _find_region(old: _Index, lo: int, hi: int) -> _Region:
    n_old = len(old.lines)
    if lo < 1 or hi > n_old:
        raise _Fallback
    group = old.tree.body
    chain: list = []
    while True:
        starts, ends = old.group_bounds(group)
        n = len(group)
        a = bisect_left(ends, lo)  # first sibling ending at/after lo
        b = bisect_right(starts, hi) - 1  # last sibling starting at/before hi
        if a <= b:
            if a == b and starts[a] <= lo and hi <= ends[a]:
                c = group[a]
                if isinstance(c, ast.ClassDef) and c.body and lo >= _start(c.body[0]):
                    chain.append(c)
                    group = c.body
                    continue
            i, j = a, b
        else:
            if a - 1 < 0 or a >= n:
                raise _Fallback
            i, j = a - 1, a
        s = min(starts[i], lo)
        e = max(ends[j], hi)
        return _Region(chain, group, i, j, s, e)


def _ws_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t\f"))]


def _fast(path, old_text, new_text, fp, runs, ipts, added_text, shift) -> Impact | None:
    for h in fp.hunks:
        for ln in h.lines:
            if ln.no_newline:
                raise _Fallback
    if not runs:
        raise _Fallback
    info = _old_info(path, old_text)  # parse errors -> generic fallback
    old = info.idx
    old_lines = old.lines
    n_old = len(old_lines)
    new_lines = new_text.split("\n")

    # PEP 263 cookie: leave the (rare) case to the exact algorithm
    o2, n2 = old_lines[:2], new_lines[:2]
    if o2 != n2 and any(_CODING_RE.match(x) for x in o2 + n2):
        raise _Fallback
    for r in runs:
        for no, t in r.removed + r.added:
            if no <= 2 and _CODING_RE.match(t):
                raise _Fallback

    # -- classify runs, build regions
    comm_ids: set[int] = set()
    regions: list[_Region] = []
    for r in runs:
        if _commentary_ok(old, r):
            comm_ids.add(id(r))
        else:
            lo, hi = _touched(r)
            regions.append(_find_region(old, lo, hi))
    regions.sort(key=lambda g: (g.s, -g.e))
    merged: list[_Region] = []
    for g in regions:
        if merged:
            p = merged[-1]
            if g.s <= p.e:
                if p.group is g.group and p.chain == g.chain:
                    p.i, p.j = min(p.i, g.i), max(p.j, g.j)
                    p.s, p.e = min(p.s, g.s), max(p.e, g.e)
                    continue
                if g.e <= p.e:  # nested in the earlier (outer) region
                    continue
                raise _Fallback
        merged.append(g)
    regions = merged
    # re-check merged same-group regions that may now overlap the next one
    for a, b in zip(regions, regions[1:]):
        if b.s <= a.e:
            raise _Fallback

    def inside(r: _Run, g: _Region) -> bool:
        if r.removed:
            return g.s <= r.removed[0][0] and r.removed[-1][0] <= g.e
        return g.s <= r.before and r.before + 1 <= g.e

    def outside(r: _Run, g: _Region) -> bool:
        if r.removed:
            return r.removed[-1][0] < g.s or r.removed[0][0] > g.e
        return r.before + 1 <= g.s or r.before >= g.e

    pure_comm: list[_Run] = []
    for r in runs:
        placed = False
        for g in regions:
            if inside(r, g):
                g.runs.append(r)
                g.delta += len(r.added) - len(r.removed)
                placed = True
                break
        if placed:
            continue
        if id(r) not in comm_ids:
            raise _Fallback
        for g in regions:
            if not outside(r, g):
                raise _Fallback
        pure_comm.append(r)

    # -- items in file order; verify unchanged segments and compute new coordinates
    items: list[tuple[float, str, object]] = []
    for g in regions:
        items.append((g.s, "r", g))
    for r in pure_comm:
        key = r.removed[0][0] if r.removed else r.before + 0.5
        items.append((key, "c", r))
    items.sort(key=lambda t: t[0])

    cum = 0
    prev_end = 0  # last old line consumed
    for _, kind, obj in items:
        if kind == "r":
            g: _Region = obj  # type: ignore[assignment]
            a_s, a_e = g.s, g.e
        else:
            r = obj  # type: ignore[assignment]
            if r.removed:
                a_s, a_e = r.removed[0][0], r.removed[-1][0]
            else:
                a_s, a_e = r.before + 1, r.before
        if a_s - 1 < prev_end:
            raise _Fallback
        if old_lines[prev_end:a_s - 1] != new_lines[prev_end + cum:a_s - 1 + cum]:
            raise _Fallback
        if kind == "r":
            g.ns = g.s + cum
            g.ne = g.e + cum + g.delta
            cum += g.delta
        else:
            r = obj  # type: ignore[assignment]
            if [t for _, t in r.removed] != old_lines[a_s - 1:a_e]:
                raise _Fallback
            ns = a_s + cum
            if [t for _, t in r.added] != new_lines[ns - 1:ns - 1 + len(r.added)]:
                raise _Fallback
            cum += len(r.added) - len(r.removed)
        prev_end = a_e
    if old_lines[prev_end:] != new_lines[prev_end + cum:]:
        raise _Fallback

    # -- verify and parse every region
    code_new: set[int] = set()
    views: list[tuple[int, int, _Index]] = []
    all_equal = True
    pos_changed = False
    for g in regions:
        _verify_region(old, g, new_lines, code_new)
        old_nodes = g.group[g.i:g.j + 1]
        eq = len(old_nodes) == len(g.new_nodes) and all(
            ast.dump(a) == ast.dump(b) for a, b in zip(old_nodes, g.new_nodes))
        if not eq:
            all_equal = False
        elif not pos_changed:
            for a, b in zip(old_nodes, g.new_nodes):
                for x, y in zip(ast.walk(a), ast.walk(b)):
                    if getattr(x, "lineno", None) != getattr(y, "lineno", None) or getattr(
                            x, "end_lineno", None) != getattr(y, "end_lineno", None):
                        pos_changed = True
                        break
                if pos_changed:
                    break

    if all_equal:
        pos_shift = pos_changed
        if not pos_shift:
            c = 0
            prev = 0
            for _, kind, obj in items:
                if kind == "r":
                    a_s, a_e, d = obj.s, obj.e, obj.delta  # type: ignore[attr-defined]
                else:
                    r = obj  # type: ignore[assignment]
                    if r.removed:
                        a_s, a_e = r.removed[0][0], r.removed[-1][0]
                    else:
                        a_s, a_e = r.before + 1, r.before
                    d = len(r.added) - len(r.removed)
                if c != 0 and _seg_has_code(old_lines, prev + 1, a_s - 1):
                    pos_shift = True
                    break
                c += d
                prev = a_e
            if not pos_shift and c != 0 and _seg_has_code(old_lines, prev + 1, n_old):
                pos_shift = True
        return Impact("noop", lineno_shift=pos_shift, comment_text=added_text,
                      insert_points=ipts, reason="identical AST")

    for g in regions:
        views.append((g.ns, g.ne, _build_view(old, g, new_lines, code_new)))
    router = _Router(views, code_new)
    return _lines_impact(old, router, runs, ipts, added_text, shift)


def _scope_risky(nodes: list) -> bool:
    """Statements outside function bodies whose compile-time validity may depend on the
    rest of the module/class scope (function bodies are compiled exactly, as a whole)."""
    stack = [n for n in nodes if not isinstance(n, _DEFS)]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return True
        if isinstance(n, ast.ImportFrom) and (
                n.module == "__future__" or any(a.name == "*" for a in n.names)):
            return True
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, _DEFS):
                stack.append(c)
    return False


def _seg_has_code(lines: list[str], a: int, b: int) -> bool:
    for k in range(max(a, 1) - 1, min(b, len(lines))):
        s = lines[k].strip()
        if s and s[0] != "#":
            return True
    return False


def _verify_region(old: _Index, g: _Region, new_lines: list[str], code_new: set[int]) -> None:
    """Parse + compile the new text of the region on its own (see module docstring)."""
    group = g.group
    first = group[g.i]
    old_first_line = old.lines[_start(first) - 1]
    ws = _ws_of(old_first_line)
    starts, ends = old.group_bounds(group)
    if first.col_offset != len(ws):  # type: ignore[attr-defined]
        raise _Fallback
    if g.i > 0 and ends[g.i - 1] >= g.s:
        raise _Fallback
    if g.j < len(group) - 1 and starts[g.j + 1] <= g.e:
        raise _Fallback
    if (g.chain and not ws) or (not g.chain and ws):
        raise _Fallback
    text = "\n".join(new_lines[g.ns - 1:g.ne])
    old_text = "\n".join(old.lines[g.s - 1:g.e])
    if "__future__" in text or "__future__" in old_text:
        raise _Fallback
    if not text.strip():
        # the region was deleted outright; fine unless it empties a class body
        if g.chain and g.i == 0 and g.j == len(g.group) - 1:
            raise _Fallback
        g.new_nodes = []
        return
    if ws:
        src = "if 1:\n" + text + "\n"
        off = g.ns - 2
    else:
        src = text + "\n"
        off = g.ns - 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src)
        if ws:
            if len(tree.body) != 1 or not isinstance(tree.body[0], ast.If) or tree.body[0].orelse:
                raise _Fallback
            nodes = tree.body[0].body
        else:
            nodes = tree.body
        for n in nodes:
            if n.col_offset != len(ws):
                raise _Fallback
        ast.increment_lineno(tree, off)
        compile(tree, "<region>", "exec", dont_inherit=True)
    if _scope_risky(nodes):
        raise _Fallback
    if ws and nodes and _ws_of(new_lines[_start(nodes[0]) - 1]) != ws:
        raise _Fallback
    g.new_nodes = nodes
    # lines carrying tokens (tokenizing the region standalone is exact: it starts and
    # ends outside any bracket/string)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text + "\n").readline):
            if tok.type in _SKIP_TOK:
                continue
            code_new.update(range(tok.start[0] + g.ns - 1, tok.end[0] + g.ns))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        raise _Fallback from None


def _build_view(old: _Index, g: _Region, new_lines: list[str], code_new: set[int]) -> _Index:
    """Tiny synthetic NEW module: shells of the enclosing classes + the parsed region."""
    nodes = list(g.new_nodes)
    body: list = nodes
    inner_old = None
    shells: list = []
    # innermost shell first
    grp = g.group
    for k in reversed(g.chain):
        sh = copy.copy(k)
        sh.end_lineno = g.ne  # type: ignore[attr-defined]
        if inner_old is None:
            first = grp[0]
            new_body = ([first] if g.i > 0 else []) + body
        else:
            new_body = ([k.body[0]] if k.body[0] is not inner_old else []) + body
        sh.body = new_body
        shells.append(sh)
        body = [sh]
        inner_old = k
    module = ast.Module(body=body, type_ignores=[])
    v = _Index(None, module, code=code_new, lines=new_lines)
    # qualified names for chain shells and region nodes
    prefix = ""
    parent: ast.AST = module
    for sh in reversed(shells):  # outermost first
        v.qual[id(sh)] = prefix + sh.name
        prefix = prefix + sh.name + "."
        parent = sh
    v._walk_groups([nodes], parent, prefix)
    v._walked = True
    return v


class _Router:
    """Routes NEW line lookups to the region view containing the line."""

    def __init__(self, views: list[tuple[int, int, _Index]], code: set[int]):
        self.views = sorted(views, key=lambda t: t[0])
        self.starts = [v[0] for v in self.views]
        self.code = code
        self.qual: dict[int, str] = {}
        for _, _, idx in self.views:
            self.qual.update(idx.qual)

    def find_unit(self, L: int):
        k = bisect_right(self.starts, L) - 1
        if k < 0 or L > self.views[k][1]:
            raise _Fallback
        return self.views[k][2].find_unit(L)

"""No-op proof for patches that only swap `is` <-> `==` / `is not` <-> `!=` in single-operator comparisons.

Facts come from maps/cmptypes.json.z (built at the pinned commit by tools/record_types.py): for every comparison
site the union over the whole test suite and all imports of (type(left), type(right)) pairs, plus per-type facts
resolved at record time (bit0 None-safe, bit1 enum-safe).  Stdlib only; sqlglot is never imported.

Proof (all must hold for every changed site, else the caller runs tests):
  (c) both operand types always NoneType                     -> `==`/`is` are both True
  (a) one side always NoneType and the other type T None-safe -> both operators give False
      None-safe = T.__eq__ and T.__ne__ resolve (by MRO) to object's or to a builtin value type's slot, which return
      NotImplemented for None (checked on samples at build time), so Python falls back to identity;
      plus sqlglot.expressions.core.Expression.__eq__/__ne__, whose source was verified at the pinned commit to be
      `self is other or (type(self) is type(other) and hash(self) == hash(other))` / `not self.__eq__(other)`
      (False / True for None; verification result stored in the map).
  (b) both sides always instances of the SAME Enum class (non-Flag, no custom __eq__/__ne__; members pairwise
      unequal) -> members are singletons, `==` iff `is`.
A site absent from the profile is accepted only if no test covers any line of it (maps.tests_for empty) and the
file is known to the coverage map.
"""
import ast
import json
import os
import zlib

_SWAP = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}
_PROFILE = None
_NONE = "builtins:NoneType"


def _load():
    global _PROFILE
    if _PROFILE is None:
        from . import maps
        d = getattr(maps, "_dir", None) or os.environ.get("BR_MAPS") or "/app/maps"
        try:
            with open(os.path.join(d, "cmptypes.json.z"), "rb") as f:
                _PROFILE = json.loads(zlib.decompress(f.read()))
        except Exception:
            _PROFILE = {}
    return _PROFILE


def _same_value(a, b):
    return type(a) is type(b) and repr(a) == repr(b)


def _diff(a, b, out):
    """Structural comparison of two ASTs; Compare nodes whose single operator was swapped are collected in out
    (old node). Returns False on any other difference."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            if (type(a.ops[0]), type(b.ops[0])) not in _SWAP:
                return False
            if not _diff(a.left, b.left, out) or not _diff(a.comparators[0], b.comparators[0], out):
                return False
            out.append(a)
            return True
        for f in a._fields:
            x, y = getattr(a, f, None), getattr(b, f, None)
            if isinstance(x, list):
                if not isinstance(y, list) or len(x) != len(y):
                    return False
                for p, q in zip(x, y):
                    if not _diff(p, q, out):
                        return False
            elif isinstance(x, ast.AST):
                if not _diff(x, y, out):
                    return False
            elif not _same_value(x, y):
                return False
        return True
    return _same_value(a, b)


def _norm(path):
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _judge(path, old_text, new_text):
    """-> (ok, [reason lines])"""
    path = _norm(path)
    if not (path.startswith("sqlglot/") and path.endswith(".py")):
        return False, ["not a sqlglot python file"]
    try:
        old, new = ast.parse(old_text), ast.parse(new_text)
    except Exception as e:  # noqa: BLE001
        return False, ["parse error: %r" % (e,)]
    nodes = []
    if not _diff(old, new, nodes):
        return False, ["ASTs differ in more than is/== swaps of single-operator comparisons"]
    if not nodes:
        return False, ["no comparison operator swapped"]
    prof = _load()
    if not prof:
        return False, ["no type profile loaded"]
    types = prof["types"]
    sites = prof["sites"].get(path, {})
    from . import maps
    reasons = []
    for n in nodes:
        key = "%d:%d:%d:%d" % (n.lineno, n.col_offset, n.end_lineno, n.end_col_offset)
        where = "%s:%d" % (path, n.lineno)
        pairs = sites.get(key)
        if pairs is None:
            try:
                if not maps.known_file(path):
                    return False, ["%s: site not profiled and file unknown to the coverage map" % where]
                cov = set()
                for ln in range(n.lineno, n.end_lineno + 1):
                    cov |= set(maps.tests_for(path, ln, "any"))
            except Exception as e:  # noqa: BLE001
                return False, ["%s: coverage lookup failed %r" % (where, e)]
            if cov:
                return False, ["%s: site never profiled but %d tests cover its lines" % (where, len(cov))]
            reasons.append("%s: never evaluated by any test or import" % where)
            continue
        for li, ri in pairs:
            ln, lf = types[li]
            rn, rf = types[ri]
            if ln == _NONE and rn == _NONE:
                continue
            if ln == _NONE or rn == _NONE:
                other, f = (rn, rf) if ln == _NONE else (ln, lf)
                if f & 1:
                    continue
                return False, ["%s: %s vs None is not None-safe (custom __eq__/__ne__)" % (where, other)]
            if li == ri and (lf & 2):
                continue
            return False, ["%s: operand types (%s, %s) are not provably identity-equal" % (where, ln, rn)]
        reasons.append("%s: %d observed type pair(s) all identity-equal: %s" % (
            where, len(pairs), "; ".join("%s/%s" % (types[a][0].split(":")[-1], types[b][0].split(":")[-1])
                                         for a, b in pairs[:6])))
    return True, reasons


def provably_equivalent(path, old_text, new_text) -> bool:
    try:
        return _judge(path, old_text, new_text)[0]
    except Exception:  # noqa: BLE001
        return False


def explain(path, old_text, new_text) -> str:
    try:
        ok, why = _judge(path, old_text, new_text)
    except Exception as e:  # noqa: BLE001
        return "not proved: internal error %r" % (e,)
    return ("proved equivalent: " if ok else "not proved: ") + " | ".join(why)

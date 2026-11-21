"""Build the class table for <root>/sqlglot (run in a fresh process; prints JSON on stdout).
usage: python tools/classtable.py ROOT > classes.json
Output: {"classes": {key: {"f": file, "s": start, "e": end, "t": tracked, "m": [root-MRO keys excl. self]}},
         "family": {key: [mate keys]}, "import_errors": [...]}"""
import ast
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import classhook  # noqa: E402


def spans(path):
    src = open(path, encoding="utf-8").read()
    out = {}

    def walk(node, stack):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                q = ".".join(stack + [ch.name])
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out[q] = (start, ch.end_lineno)
                walk(ch, stack + [ch.name])
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(ch, stack + [ch.name, "<locals>"])
            else:
                walk(ch, stack)

    walk(ast.parse(src), [])
    return out


def main():
    root = os.path.abspath(sys.argv[1])
    KEYS, USED = classhook.install(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    import unittest  # noqa: F401
    sys.path.insert(0, root)
    os.chdir(root)
    errors = []
    mods = []
    for dp, dn, fn in os.walk(os.path.join(root, "sqlglot")):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, f), root)[:-3].replace(os.sep, ".")
                mods.append(rel[:-9] if rel.endswith(".__init__") else rel)
    for m in sorted(mods):
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            errors.append(f"{m}: {e!r}")
    span_cache = {}
    classes = {}
    for cls, key in list(KEYS.items()):
        f, q = key.split("::")
        if f not in span_cache:
            span_cache[f] = spans(os.path.join(root, f))
        sp = span_cache[f].get(q)
        if sp is None:
            errors.append(f"no span for {key}")
            continue
        mro = [KEYS[b] for b in cls.__mro__[1:] if b in KEYS]
        ent = classes.setdefault(key, {"f": f, "s": sp[0], "e": sp[1], "t": 0, "m": []})
        ent["t"] = max(ent["t"], int(classhook.is_tracked(cls)))
        for k in mro:
            if k not in ent["m"]:
                ent["m"].append(k)
    # families
    from sqlglot.dialects.dialect import Dialect
    base_exposed = {getattr(Dialect, a, None) for a in
                    ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class")}
    family = {}
    for cls, key in KEYS.items():
        if not (isinstance(cls, type) and issubclass(cls, Dialect)):
            continue
        mates = {key}
        for a in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
            x = getattr(cls, a, None)
            if x is not None and x not in base_exposed and x in KEYS:
                mates.add(KEYS[x])
        for v in cls.__dict__.values():
            if isinstance(v, type) and v in KEYS:
                mates.add(KEYS[v])
        family.setdefault(key, set()).update(mates)
    json.dump({"classes": classes, "family": {k: sorted(v) for k, v in family.items()},
               "import_errors": errors}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()

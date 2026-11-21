"""Build-time: index every place the pinned tree reads a constant-style name (UPPER_CASE).
Output maps/refs.json.z: {"files": [...], "refs": {NAME: [[file_idx, stmt_first_line, mention_line], ...]}}
usage (lab container): python tools/build_refs.py /src /repo/maps"""
import ast, json, os, re, sys, zlib

root, out = sys.argv[1], sys.argv[2]
CONST = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
files, refs = [], {}
for top in ("sqlglot", "tests"):
    for d, _, fs in sorted(os.walk(os.path.join(root, top))):
        for f in sorted(fs):
            if not f.endswith(".py"):
                continue
            full = os.path.join(d, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                tree = ast.parse(open(full, encoding="utf-8").read())
            except SyntaxError:
                continue
            fi = len(files); files.append(rel)
            stack = [(tree, 0)]
            while stack:
                node, stmt = stack.pop()
                if isinstance(node, ast.stmt):
                    stmt = node.lineno
                name = None
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                    name = node.id
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    name = node.value
                if name is not None and CONST.match(name):
                    refs.setdefault(name, []).append([fi, min(stmt or node.lineno, node.lineno), node.lineno])
                for ch in ast.iter_child_nodes(node):
                    stack.append((ch, stmt))
blob = zlib.compress(json.dumps({"files": files, "refs": refs}, separators=(",", ":")).encode(), 9)
open(os.path.join(out, "refs.json.z"), "wb").write(blob)
print("names", len(refs), "mentions", sum(len(v) for v in refs.values()), "bytes", len(blob))

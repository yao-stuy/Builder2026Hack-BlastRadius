"""Build compact maps from /scratch/rec/*.json into <repo>/maps/.
usage: python tools/buildmaps.py [--rec /scratch/rec] [--out maps]"""
import argparse
import array
import json
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def varint(n, out):
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def enc_set(s):
    out = bytearray()
    prev = 0
    varint(len(s), out)
    for x in s:
        varint(x - prev, out)
        prev = x
    return bytes(out)


def build_blob(lines):
    """lines: {line: (fnset_tuple, topset_tuple)} -> bytes (uncompressed).

    Layout: u32 nsets, u32 nlines, u32 offs[nsets+1], u32 line[nlines], u32 fn_idx[nlines],
    u32 top_idx[nlines], then varint set data (count, delta-coded sorted members). Set 0 is empty."""
    sets = {(): 0}
    order = [()]

    def sid(t):
        i = sets.get(t)
        if i is None:
            i = sets[t] = len(order)
            order.append(t)
        return i

    ln = array.array("I")
    fi = array.array("I")
    ti = array.array("I")
    for line in sorted(lines):
        fn, top = lines[line]
        ln.append(line)
        fi.append(sid(fn))
        ti.append(sid(top))
    data = bytearray()
    offs = array.array("I")
    for t in order:
        offs.append(len(data))
        data += enc_set(t)
    offs.append(len(data))
    head = array.array("I", [len(order), len(ln)])
    parts = []
    for a in (head, offs, ln, fi, ti):
        if sys.byteorder != "little":
            a.byteswap()
        parts.append(a.tobytes())
    return b"".join(parts) + bytes(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", default="/scratch/rec")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "maps"))
    ap.add_argument("--tests", default="/kit/sample/tests.json")
    a = ap.parse_args()
    tests = json.load(open(a.tests))["tests"]
    n = len(tests)
    files = {}   # file -> {line: [fnlist, toplist]}
    reads = {}
    outcome = [None] * n
    cpu = [0.0] * n
    mods = [[False, False] for _ in range(n)]
    norec = []
    for t in tests:
        k = t["number"]
        p = os.path.join(a.rec, f"{k}.json")
        try:
            d = json.load(open(p))
        except Exception:
            d = {"error": "missing"}
        if "error" in d:
            norec.append(k)
            continue
        assert d["id"] == t["id"], (k, d["id"], t["id"])
        outcome[k] = d["outcome"]
        cpu[k] = d["cpu"]
        mods[k] = d.get("mods", [False, False])
        for ki, key in ((0, "fn"), (1, "top")):
            for f, ls in d[key].items():
                m = files.setdefault(f, {})
                for line in ls:
                    m.setdefault(line, ([], []))[ki].append(k)
        for r in d["reads"]:
            reads.setdefault(r, []).append(k)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    index = {}
    buf = bytearray()
    for f in sorted(files):
        lines = {ln: (tuple(v[0]), tuple(v[1])) for ln, v in files[f].items()}
        blob = zlib.compress(build_blob(lines), 9)
        index[f] = [len(buf), len(blob)]
        buf += blob
    with open(os.path.join(out, "cov.bin"), "wb") as fh:
        fh.write(buf)
    with open(os.path.join(out, "index.json"), "w") as fh:
        json.dump(index, fh, separators=(",", ":"), sort_keys=True)
    rd = json.dumps({k: v for k, v in sorted(reads.items())}, separators=(",", ":")).encode()
    with open(os.path.join(out, "reads.json.z"), "wb") as fh:
        fh.write(zlib.compress(rd, 9))
    failed = [k for k in range(n) if outcome[k] == "fail"]
    meta = {
        "version": 1,
        "tests": [t["id"] for t in tests],
        "outcome": outcome,
        "cpu": cpu,
        "imports_duckdb": [int(m[0]) for m in mods],
        "imports_pandas": [int(m[1]) for m in mods],
        "failed_isolated": failed,
        "not_recorded": norec,
    }
    with open(os.path.join(out, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    tot = 0
    for fn in sorted(os.listdir(out)):
        sz = os.path.getsize(os.path.join(out, fn))
        tot += sz
        print(f"{fn}: {sz} bytes")
    print(f"total {tot} bytes; files={len(index)} tests={n} failed_isolated={len(failed)} not_recorded={len(norec)}")
    print("failed_isolated:", failed)
    print("not_recorded:", norec)


if __name__ == "__main__":
    main()

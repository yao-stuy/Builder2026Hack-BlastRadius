"""Build-time only: download sqlglot at the pinned commit into /opt/sqlglot."""
import io, os, sys, tarfile, urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
DEST = sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot"

data = urllib.request.urlopen(URL, timeout=120).read()
os.makedirs(DEST, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
    prefix = f"sqlglot-{COMMIT}/"
    for m in tf.getmembers():
        if not m.name.startswith(prefix):
            continue
        rel = m.name[len(prefix):]
        if not rel:
            continue
        m.name = rel
        tf.extract(m, DEST, filter="data")
print("fetched sqlglot", COMMIT, "->", DEST)

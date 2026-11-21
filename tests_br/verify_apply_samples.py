"""Run in the container: compare br.diffparse.apply with `git apply` on every sample patch."""
import glob, os, shutil, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br import diffparse

SRC = "/src"
bad = 0
n = 0
for p in sorted(glob.glob("/kit/sample/commits/*.patch")):
    text = open(p, encoding="utf-8", newline="").read()
    fps = diffparse.parse(text)
    work = "/scratch/apply_chk"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    subprocess.run(["git", "init", "-q", work], check=True)
    for fp in fps:
        if fp.old_path:
            dst = os.path.join(work, fp.old_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(SRC, fp.old_path), dst)
    r = subprocess.run(["git", "apply", p], cwd=work, capture_output=True, text=True)
    if r.returncode:
        print("git apply failed", p, r.stderr); bad += 1; continue
    for fp in fps:
        old = open(os.path.join(SRC, fp.old_path), encoding="utf-8", newline="").read() if fp.old_path else ""
        mine = diffparse.apply(fp, old)
        theirs = open(os.path.join(work, fp.new_path), encoding="utf-8", newline="").read()
        n += 1
        if mine != theirs:
            print("MISMATCH", p, fp.new_path); bad += 1
print(f"{n} file patches checked, {bad} problems")
sys.exit(1 if bad else 0)

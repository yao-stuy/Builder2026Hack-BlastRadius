"""Symlink mirror of a source tree with copy-on-write patched files.

Mirror(src, dst).build()   -> dst has real dirs + symlinks to every file in src
                              (__pycache__ dirs are symlinked as a whole).
Mirror.apply(changes)      -> replace symlinks with real files for changed paths,
                              hide stale .pyc of changed modules.
Mirror.restore()           -> undo the last apply.
"""
import os
import shutil

_SKIP = {".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_PYC = "__pycache__"


def _rm(path):
    try:
        os.unlink(path)
        return
    except IsADirectoryError:
        pass
    except FileNotFoundError:
        return
    shutil.rmtree(path, ignore_errors=True)


class Mirror:
    def __init__(self, src_root, dst_root):
        self.src = os.path.abspath(src_root)
        self.dst = os.path.abspath(dst_root)
        self._files = []      # rel paths replaced/created (real files now)
        self._added_dirs = []  # rel dirs created by apply (outermost first)
        self._pyc_dirs = []   # rel dirs whose __pycache__ was materialized
        self._dirty = False

    # ------------------------------------------------------------------
    def build(self):
        if os.path.lexists(self.dst):
            _rm(self.dst)
        os.makedirs(self.dst)
        self._walk("")
        self._files, self._added_dirs, self._pyc_dirs = [], [], []
        self._dirty = False

    def _walk(self, rel):
        s = os.path.join(self.src, rel) if rel else self.src
        d = os.path.join(self.dst, rel) if rel else self.dst
        with os.scandir(s) as it:
            for e in it:
                name = e.name
                if e.is_dir(follow_symlinks=False):
                    if name in _SKIP:
                        continue
                    if name == _PYC:
                        os.symlink(e.path, os.path.join(d, name))
                        continue
                    os.mkdir(os.path.join(d, name))
                    self._walk(os.path.join(rel, name) if rel else name)
                else:
                    os.symlink(e.path, os.path.join(d, name))

    # ------------------------------------------------------------------
    def _materialize_pycache(self, reldir, exclude_stems):
        """Replace the __pycache__ symlink in reldir with a real dir of symlinks
        minus the .pyc of the given module stems."""
        dpc = os.path.join(self.dst, reldir, _PYC)
        spc = os.path.join(self.src, reldir, _PYC)
        if os.path.islink(dpc):
            os.unlink(dpc)
            os.mkdir(dpc)
            self._pyc_dirs.append(reldir)
        elif not os.path.isdir(dpc):
            # no cache in src (or new dir): nothing to hide
            return
        else:
            if reldir not in self._pyc_dirs:
                self._pyc_dirs.append(reldir)
        for n in os.listdir(dpc):
            if n.split(".", 1)[0] in exclude_stems:
                _rm(os.path.join(dpc, n))
        if os.path.isdir(spc):
            have = set(os.listdir(dpc))
            for n in os.listdir(spc):
                if n in have or n.split(".", 1)[0] in exclude_stems:
                    continue
                os.symlink(os.path.join(spc, n), os.path.join(dpc, n))

    def apply(self, changes):
        """changes: {relpath: bytes | None}. Raises on error; call restore() after."""
        self._dirty = True
        stems = {}  # reldir -> set of module stems
        for rel in changes:
            if rel.endswith(".py"):
                reldir, base = os.path.split(rel)
                stems.setdefault(reldir, set()).add(base[:-3])
        for rel, data in changes.items():
            rel = os.path.normpath(rel)
            if rel.startswith("..") or os.path.isabs(rel):
                raise ValueError("bad path " + rel)
            dp = os.path.join(self.dst, rel)
            if data is None:
                if os.path.lexists(dp):
                    self._files.append(rel)  # restore re-links if present in src
                    os.unlink(dp)
                continue
            parent = os.path.dirname(dp)
            if not os.path.isdir(parent):
                # record the outermost missing dir
                top = parent
                while not os.path.isdir(os.path.dirname(top)):
                    top = os.path.dirname(top)
                self._added_dirs.append(os.path.relpath(top, self.dst))
                os.makedirs(parent)
            self._files.append(rel)
            if os.path.lexists(dp):
                os.unlink(dp)
            with open(dp, "wb") as f:
                f.write(data)
        for reldir, st in stems.items():
            self._materialize_pycache(reldir, st)

    def restore(self):
        """Return the mirror to pristine state. Safe to call repeatedly."""
        for rel in reversed(self._files):
            dp = os.path.join(self.dst, rel)
            _rm(dp)
        for rel in self._added_dirs:
            _rm(os.path.join(self.dst, rel))
        for reldir in self._pyc_dirs:
            dpc = os.path.join(self.dst, reldir, _PYC)
            if os.path.lexists(dpc):
                _rm(dpc)
            spc = os.path.join(self.src, reldir, _PYC)
            if os.path.isdir(spc) and os.path.isdir(os.path.join(self.dst, reldir)):
                os.symlink(spc, dpc)
        for rel in self._files:
            sp = os.path.join(self.src, rel)
            dp = os.path.join(self.dst, rel)
            if os.path.isfile(sp) and os.path.isdir(os.path.dirname(dp)) and not os.path.lexists(dp):
                os.symlink(sp, dp)
        self._files, self._added_dirs, self._pyc_dirs = [], [], []
        self._dirty = False

    def cleanup(self):
        _rm(self.dst)

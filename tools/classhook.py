"""Class-usage hook (recording time only): replaces builtins.__build_class__ so that classes defined in
<root>/sqlglot/*.py get their body-defined __init__ / __new__ wrapped to record the dynamic class.

install(root) -> (KEYS, USED): KEYS maps class object -> "relpath::QualName" for every hooked class,
USED is the set of dynamic classes that were instantiated (through a wrapped __init__ / __new__).
Stdlib only; must be installed before anything from root is imported."""
import builtins
import enum
import functools
import types

KEYS = {}
USED = set()


def install(root):
    prefix = root.rstrip("/") + "/sqlglot/"
    orig = builtins.__build_class__
    add = USED.add
    FT = types.FunctionType

    def build_class(func, name, *bases, **kw):
        cls = orig(func, name, *bases, **kw)
        try:
            fn = func.__code__.co_filename
            if not (isinstance(cls, type) and fn.startswith(prefix) and fn.endswith(".py")):
                return cls
            KEYS[cls] = fn[len(root.rstrip("/")) + 1:] + "::" + cls.__qualname__
            if issubclass(cls, enum.Enum):
                return cls
            d = cls.__dict__
            init = d.get("__init__")
            if isinstance(init, FT) and not getattr(init, "_br_tracked", False):
                def mk_init(o):
                    @functools.wraps(o)
                    def w(self, *a, **k):
                        add(type(self))
                        return o(self, *a, **k)
                    w._br_tracked = True
                    return w
                type.__setattr__(cls, "__init__", mk_init(init))
            new = d.get("__new__")
            if isinstance(new, staticmethod) and isinstance(new.__func__, FT) \
                    and not getattr(new.__func__, "_br_tracked", False):
                def mk_new(o):
                    @functools.wraps(o)
                    def w(c, *a, **k):
                        add(c)
                        return o(c, *a, **k)
                    w._br_tracked = True
                    return w
                type.__setattr__(cls, "__new__", staticmethod(mk_new(new.__func__)))
        except Exception:
            pass
        return cls

    builtins.__build_class__ = build_class
    return KEYS, USED


def used_keys():
    out = set()
    for t in USED:
        k = KEYS.get(t)
        if k is None:
            for b in t.__mro__[1:]:
                k = KEYS.get(b)
                if k is not None:
                    break
        if k is not None:
            out.add(k)
    return sorted(out)


def is_tracked(cls):
    """True if some hooked class in cls's MRO has a wrapped body __init__/__new__."""
    for c in cls.__mro__:
        if c not in KEYS:
            continue
        i = c.__dict__.get("__init__")
        if getattr(i, "_br_tracked", False):
            return True
        n = c.__dict__.get("__new__")
        if isinstance(n, staticmethod) and getattr(n.__func__, "_br_tracked", False):
            return True
    return False

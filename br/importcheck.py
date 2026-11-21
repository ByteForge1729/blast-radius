"""Check whether a patch makes sqlglot crash while importing.

A class-body or module-level edit can raise at import time (for example a subclass
that pops a key the edit renamed). Every test in every file that imports the module
then fails. A fresh interpreter imports sqlglot, every dialect and the given modules,
with the patched files served from memory over the pristine tree. Takes ~0.15 s CPU.
"""

from __future__ import annotations

import json
import subprocess
import sys

CHECK = r"""
import importlib, importlib.abc, importlib.util, json, os, sys
payload = json.loads(sys.stdin.read())
root = payload["root"]
patched = {p[:-3].replace("/", ".").removesuffix(".__init__"): (p, src) for p, src in payload["files"].items()}

class Loader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path, self.src = path, src
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = os.path.join(root, self.path)
        exec(compile(self.src, module.__file__, "exec"), module.__dict__)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in patched:
            return None
        rel, src = patched[name]
        is_pkg = rel.endswith("__init__.py")
        spec = importlib.util.spec_from_loader(name, Loader(rel, src), origin=os.path.join(root, rel), is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = [os.path.dirname(os.path.join(root, rel))]
        return spec

sys.meta_path.insert(0, Finder())
sys.path.insert(0, root)
names = ["sqlglot"] + payload["modules"] + list(patched)
import sqlglot
from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES
names += ["sqlglot.dialects." + k for k in DIALECT_MODULE_NAMES]
for name in names:
    if name.startswith("tests"):
        continue
    importlib.import_module(name)
print("ok")
"""


def import_crashes(src_root: str, patched: dict[str, str], extra_modules: list[str]) -> bool:
    """True if importing sqlglot with `patched` ({rel path: new source}) raises."""
    payload = json.dumps({"root": src_root, "files": patched, "modules": extra_modules})
    try:
        proc = subprocess.run(
            [sys.executable, "-c", CHECK], input=payload, capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return proc.returncode != 0 or proc.stdout.strip() != "ok"

"""Decide which tests one changed file can affect, using coverage and fixture maps.

Precise path: a changed statement inside a function or lambda selects the tests that
executed that statement. Anything evaluated at import time (class or module level,
new functions) falls back to the tests that touched the class, module, or dialect
family. Unknowns select everything (ALL).
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

from br.diff import FileDiff, Hunk, apply_hunks

ALL = None  # sentinel meaning "every test"
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
NAMED_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg", ".html")
FAMILY_DIRS = ("dialects", "parsers", "generators", "typing")
FAMILY_SKIP = ("__init__", "dialect", "base")
PACKAGE_WALKERS = {"sqlglot/typing/": "tests/test_typing.py"}
WHOLE_FILE = (0, 10**9)


@dataclass
class Maps:
    coverage: dict[str, dict[int, list[int]]]
    fixtures: dict[str, list[int]]
    tests_by_file: dict[str, set[int]]
    classes: dict[str, list[int]] = field(default_factory=dict)


def _start(node: ast.AST) -> int:
    decos = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decos])


def _chain(tree: ast.AST, line: int) -> list[ast.AST]:
    """Nodes (outermost first) whose line range contains `line`."""
    chain: list[ast.AST] = []
    node = tree
    while True:
        nxt = None
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or getattr(child, "end_lineno", None) is None:
                continue
            if _start(child) <= line <= child.end_lineno:
                nxt = child
                if isinstance(child, (ast.stmt, ast.Lambda)):
                    break
        if nxt is None:
            return chain
        chain.append(nxt)
        node = nxt


def _qualname(chain: list[ast.AST]) -> tuple[str, ...]:
    return tuple(n.name for n in chain if isinstance(n, SCOPE_TYPES))


def _scopes(tree: ast.AST) -> dict[tuple[str, ...], ast.AST]:
    out: dict[tuple[str, ...], ast.AST] = {}

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPE_TYPES):
                name = prefix + (child.name,)
                out.setdefault(name, child)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, ())
    return out


def _own_lines(stmt: ast.stmt) -> tuple[int, int]:
    """Lines a statement executes itself: a simple statement's whole span, or a compound
    statement's header (condition, iterator, context managers) up to its first child."""
    children = [
        child.lineno
        for field in ("body", "handlers", "orelse", "finalbody", "cases")
        for child in getattr(stmt, field, None) or []
        if hasattr(child, "lineno")
    ]
    if not children:
        return _start(stmt), stmt.end_lineno
    return _start(stmt), max(_start(stmt), min(children) - 1)


def _code_line(lines: list[str], idx: int, step: int) -> int | None:
    """Nearest 1-based line from idx (inclusive) in direction `step` that holds code."""
    while 1 <= idx <= len(lines):
        text = lines[idx - 1].strip()
        if text and not text.startswith("#"):
            return idx
        idx += step
    return None


def _is_code(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#")


def _union(parts) -> set[int] | None:
    out: set[int] = set()
    for part in parts:
        if part is ALL:
            return ALL
        out |= part
    return out


def _added(hunk: Hunk) -> list[tuple[int, int]]:
    """(old insertion point, new line number) for every '+' line, single-hunk application."""
    out = []
    old_cursor = hunk.old_start if hunk.old_len else hunk.old_start + 1
    new_cursor = old_cursor
    for line in hunk.lines:
        if line.startswith("+"):
            out.append((old_cursor, new_cursor))
            new_cursor += 1
        elif line.startswith("-"):
            old_cursor += 1
        else:
            old_cursor += 1
            new_cursor += 1
    return out


class Analyzer:
    def __init__(self, src_root: str, maps: Maps) -> None:
        self.src_root = src_root
        self.maps = maps

    # ---- coverage helpers -------------------------------------------------
    def _lines(self, path: str, lines) -> set[int]:
        cov = self.maps.coverage.get(path, {})
        out: set[int] = set()
        for line in lines:
            out.update(cov.get(line, ()))
        return out

    def _range(self, path: str, start: int, end: int) -> set[int]:
        out: set[int] = set()
        for line, tests in self.maps.coverage.get(path, {}).items():
            if start <= line <= end:
                out.update(tests)
        return out

    def _module(self, path: str) -> set[int] | None:
        tests = self._range(path, *WHOLE_FILE) | self.maps.tests_by_file.get(path, set())
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILY_DIRS:
            name = parts[2][:-3]
            if name not in FAMILY_SKIP:
                for folder in FAMILY_DIRS:
                    tests |= self._range(f"sqlglot/{folder}/{name}.py", *WHOLE_FILE)
                tests |= self.maps.tests_by_file.get(f"tests/dialects/test_{name}.py", set())
        return tests or ALL

    def _broad(self, path: str, classes: list[ast.AST]) -> set[int] | None:
        """Tests that can see an import-time change in `path` (inside the innermost class)."""
        if path.startswith("tests/") and path in self.maps.tests_by_file:
            return set(self.maps.tests_by_file[path])
        if classes and path.startswith("sqlglot/expressions/"):
            key = f"{path}::{'.'.join(c.name for c in classes)}"
            used = set(self.maps.classes.get(key, ()))
            in_class = self._range(path, _start(classes[-1]), classes[-1].end_lineno)
            if used or in_class:
                return used | in_class
        module = self._module(path)
        if module is ALL or not classes:
            return module
        in_class = self._range(path, _start(classes[-1]), classes[-1].end_lineno)
        is_family = path.count("/") == 2 and path.split("/")[1] in FAMILY_DIRS
        return (in_class | module) if is_family else (in_class or module)

    # ---- per-line analysis on the old tree --------------------------------
    def _old_line(self, path: str, tree: ast.AST, line: int) -> set[int] | None:
        chain = _chain(tree, line)
        funcs = [i for i, n in enumerate(chain) if isinstance(n, FUNC_TYPES)]
        if not funcs:
            return self._broad(path, [n for n in chain if isinstance(n, ast.ClassDef)])
        idx = funcs[-1]
        func = chain[idx]
        if isinstance(func, ast.Lambda):
            return self._range(path, func.lineno, func.end_lineno)
        if line < func.body[0].lineno:  # decorator, signature, defaults
            return self._range(path, _start(func), func.end_lineno)
        stmts = [n for n in chain[idx + 1:] if isinstance(n, ast.stmt)]
        if not stmts:
            return self._lines(path, {line})
        start, end = _own_lines(stmts[-1])
        return self._range(path, min(start, line), max(end, line))

    # ---- per-insertion analysis on the new tree ---------------------------
    def _insertion(self, path, old_tree, old_lines, new_tree, new_line: int, point: int):
        chain = _chain(new_tree, new_line)
        funcs = [n for n in chain if isinstance(n, FUNC_TYPES)]
        if not funcs:
            return self._broad_new(path, old_tree, chain)
        named = [n for n in chain if isinstance(n, NAMED_FUNCS)]
        if named:
            qual = _qualname(chain[: chain.index(named[-1]) + 1])
            old_func = _scopes(old_tree).get(qual)
            if old_func is None:
                return self._broad_new(path, old_tree, chain)
            if funcs[-1] is named[-1] and new_line < named[-1].body[0].lineno:
                return self._range(path, _start(old_func), old_func.end_lineno)
        neighbours = (_code_line(old_lines, point - 1, -1), _code_line(old_lines, point, 1))
        return _union(self._old_line(path, old_tree, n) for n in neighbours if n is not None)

    def _broad_new(self, path: str, old_tree, new_chain) -> set[int] | None:
        classes = [n for n in new_chain if isinstance(n, ast.ClassDef)]
        if classes:
            qual = _qualname(new_chain[: new_chain.index(classes[-1]) + 1])
            old_cls = _scopes(old_tree).get(qual)
            if old_cls is not None:
                return self._broad(path, [old_cls])
        return self._broad(path, [])

    # ---- file-level entry points ------------------------------------------
    def tests_for(self, fd: FileDiff) -> set[int] | None:
        path = fd.path
        if path.startswith("tests/fixtures/"):
            return self._fixture(fd)
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            return self._python(fd)
        if path.endswith(DOC_SUFFIXES) or path.startswith(("docs/", ".github/")):
            return set()
        return ALL

    def _fixture(self, fd: FileDiff) -> set[int] | None:
        if fd.path.endswith(".sql") and not fd.binary and fd.old_path and fd.new_path:
            changed = [t for h in fd.hunks for t in h.changed_text()]
            if all(not t or t.startswith("--") for t in changed):
                return set()
        tests = self.maps.fixtures.get(fd.old_path or "") or self.maps.fixtures.get(fd.new_path or "")
        if tests:
            return set(tests)
        folder = os.path.dirname(fd.path) + "/"
        near = {n for f, ts in self.maps.fixtures.items() if f.startswith(folder) for n in ts}
        return near or ALL

    def _python(self, fd: FileDiff) -> set[int] | None:
        path = fd.path
        extra: set[int] = set()
        for prefix, test_file in PACKAGE_WALKERS.items():
            if path.startswith(prefix):
                extra |= self.maps.tests_by_file.get(test_file, set())
        if fd.old_path is None:
            return extra  # a new module nothing imports yet
        if fd.new_path is None or fd.old_path != fd.new_path:
            return ALL  # deleted or renamed module
        try:
            with open(os.path.join(self.src_root, path), encoding="utf-8") as handle:
                old_text = handle.read()
            old_tree = ast.parse(old_text)
        except (OSError, SyntaxError, ValueError):
            return ALL
        old_lines = old_text.split("\n")
        old_dump = ast.dump(old_tree)
        try:
            full_dump = ast.dump(ast.parse("\n".join(apply_hunks(old_lines, fd.hunks))))
        except (SyntaxError, ValueError):
            return self._broken(path)
        if full_dump == old_dump:
            return set()
        tests = _union(self._hunk(path, old_tree, old_dump, old_lines, h) for h in fd.hunks)
        return ALL if tests is ALL else tests | extra

    def _broken(self, path: str) -> set[int] | None:
        if path.startswith(("tests/test_", "tests/dialects/test_")):
            return set(self.maps.tests_by_file.get(path, set()))
        return ALL

    def _hunk(self, path, old_tree, old_dump, old_lines, hunk: Hunk):
        new_lines = apply_hunks(old_lines, [hunk])
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except (SyntaxError, ValueError):
            return self._broken(path)
        if ast.dump(new_tree) == old_dump:
            return set()
        parts = [self._old_line(path, old_tree, line) for line in hunk.removed_lines()]
        for point, new_line in _added(hunk):
            if _is_code(new_lines[new_line - 1]):
                parts.append(self._insertion(path, old_tree, old_lines, new_tree, new_line, point))
        return _union(parts)

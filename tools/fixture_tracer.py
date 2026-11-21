"""Pytest plugin: record which files under tests/fixtures each test opens.

Used only offline to build maps/fixtures.json from the pinned sqlglot commit.
Run with: pytest -p fixture_tracer (tools/ on PYTHONPATH). Output path comes
from the FIXTURE_TRACE_OUT environment variable.
"""

import builtins
import concurrent.futures
import io
import json
import os
import pickle

import pytest

_real_open = builtins.open
_real_io_open = io.open
_current: list[str] = []
_seen: dict[str, set[str]] = {}


def _record(path) -> None:
    if not _current:
        return
    try:
        resolved = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    marker = os.sep + "tests" + os.sep + "fixtures" + os.sep
    if marker in resolved:
        rel = "tests/fixtures/" + resolved.split(marker, 1)[1].replace(os.sep, "/")
        _seen.setdefault(_current[0], set()).add(rel)


def _traced_open(file, *args, **kwargs):
    _record(file)
    return _real_open(file, *args, **kwargs)


def _roundtrip(value):
    """Pickle and unpickle, as a real process pool would (sqlglot pickles via serde)."""
    return pickle.loads(pickle.dumps(value))


class InlineExecutor:
    """Drop-in ProcessPoolExecutor that runs work in-process so coverage sees it."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = _roundtrip((args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as error:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(error)
        return future

    def shutdown(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_classes: dict[str, set[str]] = {}


def _class_keys(cls) -> list[str]:
    keys = []
    for klass in cls.__mro__:
        module = getattr(klass, "__module__", "") or ""
        if module.startswith("sqlglot.expressions"):
            keys.append(module.replace(".", "/") + ".py::" + klass.__qualname__)
    return keys


def _wrap_init(klass) -> None:
    original = klass.__dict__.get("__init__")
    if original is None:
        return

    def __init__(self, *args, **kwargs):
        if _current:
            _classes.setdefault(_current[0], set()).update(_class_keys(type(self)))
        return original(self, *args, **kwargs)

    klass.__init__ = __init__


def _record_expression_classes() -> None:
    """Record which expression classes each test constructs (class-level edits reach them)."""
    from sqlglot.expressions import core

    for name in ("Expr", "Expression"):
        if hasattr(core, name):
            _wrap_init(getattr(core, name))


def pytest_configure(config):
    if os.environ.get("INLINE_POOLS"):
        concurrent.futures.ProcessPoolExecutor = InlineExecutor
    builtins.open = _traced_open
    io.open = _traced_open
    if os.environ.get("TRACE_CLASSES"):
        _record_expression_classes()


def pytest_unconfigure(config):
    builtins.open = _real_open
    io.open = _real_io_open


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Files a test module opens while importing belong to every test in that module."""
    if isinstance(collector, pytest.Module):
        _current[:] = [f"collect:{collector.nodeid}"]
        try:
            yield
        finally:
            _current.clear()
    else:
        yield


def _phase(item, phase: str):
    _current[:] = [f"{item.nodeid}|{phase}"]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _phase(item, "setup")
    yield
    _current.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _phase(item, "run")
    yield
    _current.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _phase(item, "teardown")
    yield
    _current.clear()


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("FIXTURE_TRACE_OUT")
    if not out:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with _real_open(f"{out}.{worker}.json", "w", encoding="utf-8") as handle:
        json.dump({k: sorted(v) for k, v in _seen.items()}, handle, indent=0, sort_keys=True)
    with _real_open(f"{out}-classes.{worker}.json", "w", encoding="utf-8") as handle:
        json.dump({k: sorted(v) for k, v in _classes.items()}, handle, indent=0, sort_keys=True)

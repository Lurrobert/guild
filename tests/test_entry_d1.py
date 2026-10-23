"""entry.D1: a dropped D1 read is retried; a write is not."""
import asyncio
import sys
import types

import pytest


def load_entry(monkeypatch):
    for name in ("js", "pyodide", "pyodide.ffi", "workers"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    js = sys.modules["js"]
    for attr in ("Headers", "Object", "Response", "caches", "fetch"):
        setattr(js, attr, object())
    sys.modules["pyodide.ffi"].to_js = lambda *a, **k: None
    sys.modules["workers"].WorkerEntrypoint = object
    monkeypatch.delitem(sys.modules, "entry", raising=False)
    import entry
    monkeypatch.setattr(entry, "READ_BACKOFF_S", 0)
    return entry


class Stmt:
    def __init__(self, binding):
        self.binding = binding

    def bind(self, *args):
        return self

    async def _go(self, value):
        self.binding.calls += 1
        if self.binding.calls <= self.binding.fail:
            raise RuntimeError("D1_ERROR: Network connection lost.")
        return value

    def first(self):
        return self._go({"n": 1})

    def all(self):
        return self._go({"results": [{"n": 1}]})

    def run(self):
        return self._go({"meta": {"changes": 1}})


class Binding:
    def __init__(self, fail):
        self.fail, self.calls = fail, 0

    def prepare(self, sql):
        return Stmt(self)


def test_reads_retry_a_dropped_request(monkeypatch):
    entry = load_entry(monkeypatch)
    db = entry.D1(Binding(fail=2))
    assert asyncio.run(db.first("SELECT 1")) == {"n": 1}
    db = entry.D1(Binding(fail=2))
    assert asyncio.run(db.all("SELECT 1")) == [{"n": 1}]


def test_reads_give_up_after_three_tries_and_writes_never_retry(monkeypatch):
    entry = load_entry(monkeypatch)
    binding = Binding(fail=3)
    with pytest.raises(RuntimeError):
        asyncio.run(entry.D1(binding).first("SELECT 1"))
    assert binding.calls == 3
    binding = Binding(fail=1)
    with pytest.raises(RuntimeError):
        asyncio.run(entry.D1(binding).run("UPDATE x SET y = 1"))
    assert binding.calls == 1

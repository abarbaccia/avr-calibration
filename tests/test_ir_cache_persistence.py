"""Tests for impulse-IR cache persistence across container restart (Fix 2)."""

from calibrate import mcp_server as sut


def test_persist_and_load_ir_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sut, "_ir_cache_dir", lambda: tmp_path)
    ir = [0.0, 0.5, -0.25, 1.0]
    sut._persist_ir(7, ir)
    loaded = sut._load_ir_from_disk(7)
    assert loaded == ir


def test_load_ir_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sut, "_ir_cache_dir", lambda: tmp_path)
    assert sut._load_ir_from_disk(999) is None


def test_get_cached_ir_recovers_from_disk_after_reset(tmp_path, monkeypatch):
    """Simulate a container restart: in-memory cache is cleared but the IR
    was persisted to disk, so the read path recovers it."""
    monkeypatch.setattr(sut, "_ir_cache_dir", lambda: tmp_path)
    ir = [0.1, 0.2, 0.3]
    # Write through both memory + disk as the real measure path does.
    sut._ir_cache[42] = ir
    sut._persist_ir(42, ir)

    # Container restart wipes the in-memory cache.
    sut._ir_cache.clear()
    assert 42 not in sut._ir_cache

    recovered = sut._get_cached_ir(42)
    assert recovered == ir
    # Fast path is repopulated.
    assert sut._ir_cache.get(42) == ir


def test_get_cached_ir_prefers_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(sut, "_ir_cache_dir", lambda: tmp_path)
    sut._ir_cache.clear()
    sut._ir_cache[1] = [9.0]
    # Disk has a stale/different copy; memory should win.
    sut._persist_ir(1, [0.0])
    assert sut._get_cached_ir(1) == [9.0]

# Testing

100% test coverage is the key to great vibe coding. Tests let you move fast, trust your instincts, and ship with confidence — without them, vibe coding is just yolo coding. With tests, it's a superpower.

## Framework

**pytest** + **pytest-asyncio** + **respx** (httpx mocking)

Version: pytest 9.x, pytest-asyncio 1.x

## Running tests

```bash
# All tests
uv run python -m pytest tests/ -v

# With coverage
uv run python -m pytest tests/ -v --cov --cov-report=term-missing

# Single file
uv run python -m pytest tests/test_preflight.py -v
```

## Test layers

### Unit tests (`tests/test_*.py`)
Test individual functions and methods in isolation. All external dependencies (hardware, HTTP, audio) are mocked. These run in ~0.2s with no hardware required.

### Integration tests (future)
End-to-end loop tests with mock hardware adapters. Will cover full calibration cycles without real hardware.

### Live tests (manual)
Run against real hardware after `calibrate check` passes. Not automated — run manually before major releases.

## Conventions

- **File naming:** `tests/test_{module}.py` mirrors `calibrate/{module}.py`
- **Class grouping:** Test methods grouped in classes by the function under test (e.g., `TestMicCheck`, `TestMinidspCheck`)
- **Async tests:** All async test methods are automatically handled by `pytest-asyncio` (mode=auto in `pyproject.toml`)
- **sounddevice mocking:** `sounddevice` is injected into `sys.modules` via a session-scoped fixture in `conftest.py` (avoids PortAudio dependency in CI)
- **pytta mocking:** `pytta` is injected into `sys.modules` the same way — `fake_pytta_module` fixture; individual tests set return values and call `reset_mock()` to clear cross-test call history
- **HTTP mocking:** Use `respx` for mocking httpx calls to minidspd
- **Denon mocking:** Use `unittest.mock.patch("denonavr.DenonAVR", ...)` with `AsyncMock` for `async_setup`
- **numpy:** Used directly (not mocked) — real FFT computation in `_compute_fr` tests; mock signals via `numpy.random`

## Adding tests

When a new function is added to `calibrate/`, add a corresponding test class to the matching `tests/test_*.py` file. Cover:
- Happy path
- Each error branch
- Edge cases (None values, empty lists, connection failures)

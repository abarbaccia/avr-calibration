<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/feat-equipment-driver-abstraction-autoplan-restore-20260401-022023.md -->

# Plan: Equipment Driver Abstraction

**Branch:** `feat/equipment-driver-abstraction`
**Base:** `feat/mcp-server`
**Feature Brief:** `~/.gstack/projects/abarbaccia-avr-calibration/andrew-feat-equipment-driver-abstraction-feature-20260401-0218.md`

## Problem

`calibrate/mcp_server.py` (635 lines) has `denonavr` imports and Denon-specific logic hardcoded in tool implementations. The miniDSP adapter (`calibrate/adapters/minidsp.py`) is a good model — it's a dedicated module — but the Denon equivalent doesn't exist. A user with a Yamaha AVR would need to modify `mcp_server.py` directly. Recipes that call `set_denon_volume` would break on any non-Denon setup.

## Goal

A driver protocol layer so:
1. MCP tools call `avr_driver.set_volume()` / `dsp_driver.apply_eq()` — no brand references in `mcp_server.py`
2. The right driver loads at startup based on `config.yaml` (`avr_driver: denon`, `dsp_driver: minidsp`)
3. Adding a new driver requires: create a class implementing the protocol + register one config key

## Architecture

### New file structure

```
calibrate/
  drivers/
    __init__.py
    avr_driver.py       # AVRDriver ABC: setup, get_state, list_inputs, set_input, set_volume
    dsp_driver.py       # DSPDriver ABC: get_state, read_eq, apply_eq, set_preset, set_routing, current_preset
    denon.py            # DenonDriver(AVRDriver) — wraps denonavr
    minidsp.py          # MinidspDriver(DSPDriver) — wraps MinidspClient from adapters/
    registry.py         # load_avr_driver(config) + load_dsp_driver(config) factories
  mcp_server.py         # (modified) — calls driver methods, zero denonavr/MinidspClient references
  config.py             # (modified) — add avr_driver/dsp_driver keys + defaults
```

### Driver protocols

Narrowed to only what MCP tools currently call (CEO review: no speculative interface).

```python
# avr_driver.py
class AVRDriver(ABC):
    async def get_state(self) -> dict: ...            # get_device_state tool
    async def set_volume(self, level_db: float) -> None: ...  # avr_set_volume tool
    async def close(self) -> None: ...               # teardown on server shutdown
    # Non-abstract (override in subclass when available):
    async def discover(self) -> list[str]: return []  # SSDP/mDNS discovery

# dsp_driver.py
class DSPDriver(ABC):
    async def get_state(self) -> dict: ...           # get_device_state tool
    async def current_preset(self) -> int: ...       # used internally by read_eq/apply_eq
    async def read_eq(self, preset: int) -> list[dict]: ...   # read_eq tool
    async def apply_eq(self, preset: int, filters: list[dict]) -> None: ...  # apply_eq tool
    async def set_preset(self, preset: int) -> None: ...      # signal path
    async def set_routing(self, routing: dict) -> None: ...   # signal path
    async def close(self) -> None: ...               # teardown on server shutdown
```

Error boundary: each driver method raises `DriverError(message)`. MCP tool functions catch `DriverError` and return `_err(str(exc))`. No raw exceptions escape to MCP protocol layer.

### MCP tool renames

| Old name | New name | Reason |
|---|---|---|
| `set_denon_volume` | `avr_set_volume` | Brand-agnostic |
| `get_device_state` | `get_device_state` | Keep — already generic |
| `read_eq` | `read_eq` | Keep — recipe references this |
| `apply_eq` | `apply_eq` | Keep — recipe references this |

**Backward-compat alias:** `set_denon_volume` remains as a deprecated alias → calls `avr_set_volume`. Prevents breaking any existing Claude Code sessions that have cached the old tool name. Alias marked deprecated in description. Remove after one release cycle.

### Config additions

```yaml
avr_driver: denon    # which AVRDriver implementation to load (default: denon)
dsp_driver: minidsp  # which DSPDriver implementation to load (default: minidsp)
```

### Driver registry

```python
# registry.py
_AVR_DRIVERS = {"denon": DenonDriver}
_DSP_DRIVERS = {"minidsp": MinidspDriver}

def load_avr_driver(config: Config) -> AVRDriver: ...
def load_dsp_driver(config: Config) -> DSPDriver: ...
```

### In-memory EQ state

Currently lives as `_eq_state: dict[int, list[dict]]` at module level in `mcp_server.py`. Moves into `MinidspDriver` as an instance variable. `_get_eq_state` / `_set_eq_state` become `driver.read_eq_state()` / `driver.write_eq_state()`.

### MCP server startup

```python
# mcp_server.py — module level, loaded once
_cfg = Config.load()
_avr: AVRDriver = load_avr_driver(_cfg)
_dsp: DSPDriver = load_dsp_driver(_cfg)
```

## Implementation Steps

1. `calibrate/drivers/__init__.py` — empty, marks package
2. `calibrate/drivers/avr_driver.py` — `AVRDriver` ABC
3. `calibrate/drivers/dsp_driver.py` — `DSPDriver` ABC
4. `calibrate/drivers/denon.py` — `DenonDriver(AVRDriver)` (move Denon logic from mcp_server.py)
5. `calibrate/drivers/minidsp.py` — `MinidspDriver(DSPDriver)` (wrap `adapters.minidsp.MinidspClient`; owns `_eq_state`)
6. `calibrate/drivers/registry.py` — `load_avr_driver()` + `load_dsp_driver()`
7. `calibrate/config.py` — add `avr_driver` / `dsp_driver` to `DEFAULT_CONFIG`; add `Config.avr_driver_name` and `Config.dsp_driver_name` typed properties; bundle TODO-SP1 (atomic YAML write: `os.replace` in `update_config()`)
8. `calibrate/mcp_server.py` — replace all inline Denon/miniDSP logic with driver calls; rename `set_denon_volume` → `avr_set_volume`; add backward-compat `set_denon_volume` alias; add asyncio lock around `_eq_state` writes (now inside MinidspDriver); fix config-per-call (load once at startup); add `DriverError` handling in each tool function
9. `tests/test_mcp_server.py` — mock drivers at the driver level (not `denonavr` / `MinidspClient`)
10. `tests/test_drivers.py` — new: unit tests for `DenonDriver` + `MinidspDriver` + registry

## Out of Scope

- Implementing any second driver (Yamaha, other DSP)
- Auto-discovery of hardware type
- MCP config tools (get_config / set_config)
- Updating recipes — `apply_eq` keeps its name; `harman-bass.md` unchanged

## Done Criteria

- All test BEHAVIORS preserved (test file updated to mock at driver level; `sys.modules["denonavr"]` hack replaced with `patch("calibrate.mcp_server._avr", mock_driver)`)
- `mcp_server.py` contains zero direct references to `denonavr` or `MinidspClient`
- `avr_set_volume` is the primary tool; `set_denon_volume` exists as deprecated alias
- Adding a new AVR brand requires only: subclass `AVRDriver` + add one entry to `_AVR_DRIVERS`
- 100% test coverage maintained (new `tests/test_drivers.py` + updated `tests/test_mcp_server.py`)

## Eng Review Additions (autoplan 2026-04-01)

### P0: Partial write rollback required in MinidspDriver.apply_eq

`_tool_apply_eq` writes PEQ slots one at a time across outputs 0 and 1. A `MinidspApiError` mid-loop leaves hardware partially configured. `_eq_state` must only update after ALL writes succeed. `MinidspDriver.apply_eq` must: acquire the asyncio lock → write all slots → on any exception, do NOT update `_eq_state` → re-raise as `DriverError`. The SafetyValidator diffs against `_eq_state`; if state diverges from hardware, safety limits can be silently violated.

### P0: Path traversal — add symlink resolution check

`fetch_recipe` currently checks for `".."` in the name but does not block symlinks inside `recipes/` that point outside it. Add after constructing `recipe_path`:
```python
if not recipe_path.resolve().is_relative_to(RECIPES_DIR.resolve()):
    return _err(f"invalid recipe name: {name!r}")
```
Test: `recipes/evil -> /etc/passwd` symlink → returns `_err`.

### P1: Config loading — keep per-call (hot-reload is a feature)

Do NOT change `_config()` to a startup singleton. Config.load() is a fast local file read (~1ms). Removing it would silently kill hot-reload (user edits `config.yaml` while server runs). The original plan step 8 said "fix config-per-call" — **REVERTED**. Keep `_config()` as-is.

### P1: asyncio lock in MinidspDriver.apply_eq — full sequence

The lock must wrap: `read _eq_state` → `SafetyValidator` → `write hardware` → `update _eq_state`. If lock only wraps the state update, two concurrent `apply_eq` calls can both pass SafetyValidator with the same baseline before either writes. The lock is an instance-level `asyncio.Lock()` on `MinidspDriver`.

### P1: Starlette lifespan handler — required for close()

Add to `create_app()`:
```python
from contextlib import asynccontextmanager
@asynccontextmanager
async def lifespan(app):
    await _avr.setup()   # async_setup with 5s timeout
    await _dsp.setup()
    yield
    await _avr.close()
    await _dsp.close()
Starlette(lifespan=lifespan, routes=[...])
```
`DenonDriver.__init__` must be sync (no network). `setup()` is called in lifespan, not constructor — prevents blocking cold start when AVR is off.

### P1: DriverError exception hierarchy

Define `DriverError(RuntimeError)` in `calibrate/drivers/base.py`. All driver methods raise `DriverError` (wrapping `MinidspApiError`, `denonavr` exceptions, `asyncio.TimeoutError`). All MCP tool functions catch `DriverError` and return `_err(str(exc))`. No raw hardware exceptions escape to the MCP protocol layer.

### P2: Deprecated alias must appear in list_tools()

Keep `Tool(name="set_denon_volume", description="Deprecated: use avr_set_volume. ...")` in `_TOOLS` so Claude Code sessions that cached the old name still see it in the tool list. Remove in the next release cycle.

### P2: update_config() atomic write — REQUIRED (not optional)

This is mandatory, not a deferred TODO. Four-line fix in step 7. Write to `path.with_suffix('.tmp')`, then `os.replace(tmp, path)`.

### P2: eq://current resource is 3rd MinidspClient call site

`_read_resource("eq://current")` at line 565 calls `_minidsp_client()` directly. After the refactor, this must call `_dsp.current_preset()` instead. Add CI grep check: `grep -n "MinidspClient" calibrate/mcp_server.py` must return 0 lines.

### P3: reset_eq_state fixture breaks post-refactor

`tests/test_mcp_server.py:66` clears `_eq_state` directly. After `_eq_state` moves into `MinidspDriver`, this fixture silently stops working. Replace with: mock the driver's EQ state as part of the mock driver fixture.

## Known Risks (updated)

- 605-line test file uses 4 brittle patterns (sys.modules injection, _eq_state direct access, _config patch, private function imports) — all must be replaced in step 9
- `_minidsp_client()` has 3 call sites in mcp_server.py (lines 198, 228, 565) — all 3 must be replaced; add CI grep check
- Test for concurrent `apply_eq` requires `asyncio.gather` — must be a real async test, not a mock


<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Narrow ABC interface to only what MCP tools currently call; remove list_inputs, set_input | Mechanical | P5 explicit | Speculative interface for non-existent use cases; add when needed | Keep full API |
| 2 | CEO | Add backward-compat alias set_denon_volume → avr_set_volume | Mechanical | P1 completeness | Breaks cached Claude Code sessions otherwise | Remove immediately |
| 3 | CEO | Fix done criteria: "tests unchanged" impossible; "behaviors preserved, tests updated" | Mechanical | P1 completeness | test_mcp_server.py imports _tool_set_denon_volume which will be removed | Keep old criteria |
| 4 | CEO | Bundle TODO-SP1 (atomic YAML write) into step 7 | Mechanical | P2 boil lakes | config.py is in blast radius; 4-line fix; prevents config corruption on Pi power loss | Defer to separate PR |
| 5 | CEO | Add Config.avr_driver_name / Config.dsp_driver_name typed properties | Mechanical | P1 completeness | Registry must not access Config._data directly | Use _data.get() |
| 6 | CEO | Add close() and discover() to AVRDriver ABC | Mechanical | P1 completeness | Lifecycle and discovery are part of the driver contract | Driver-specific only |
| 7 | CEO | DEFER: MCP get_config/set_config tools | Taste | P3 pragmatic | Separate feature; not in this brief | Include now |
| 8 | CEO | DEFER: discover_avr MCP tool | Taste | P3 pragmatic | Separate feature; separate PR | Include now |
| 9 | CEO/Taste | Proceed with abstraction now (no second user yet) | Taste | User decision | User explicitly chose architectural investment before recipes lock in brand names | Defer |
| 10 | CEO/Taste | 6-month regret: automated measurement loop deferred | Taste | User decision | User chose driver abstraction as higher priority | Switch to measurement loop |
| 11 | Eng | P0: MinidspDriver.apply_eq must rollback on partial failure | Mechanical | P1 completeness | Partial write leaves hardware/state diverged; SafetyValidator diffs against wrong baseline | Accept divergence |
| 12 | Eng | P0: Add resolve().is_relative_to() for path traversal | Mechanical | Security | String check alone doesn't block symlinks | String check only |
| 13 | Eng | P1: REVERT config singleton change; keep _config() per-call | Mechanical | P3 pragmatic | Hot-reload is a feature; Config.load() is 1ms on local file | Singleton |
| 14 | Eng | P1: asyncio lock must wrap full read-validate-write-state sequence | Mechanical | P1 completeness | Narrow lock still allows SafetyValidator race between two concurrent calls | Lock only state write |
| 15 | Eng | P1: Add Starlette lifespan handler for setup()/close() | Mechanical | P1 completeness | DenonDriver.close() never called without it; aiohttp session leaks | Skip teardown |
| 16 | Eng | P1: Define DriverError in drivers/base.py | Mechanical | P5 explicit | Referenced in plan without existing; MinidspApiError must wrap into it | Reuse MinidspApiError |
| 17 | Eng | P2: Deprecated alias in _TOOLS (not just dispatch) | Mechanical | P1 completeness | Claude Code checks list_tools(); alias at dispatch only is invisible | Dispatch-only alias |
| 18 | Eng | P2: update_config() atomic write REQUIRED | Mechanical | P1 completeness | Non-atomic write + Pi power loss = zero-byte config; 4-line fix | Optional/deferred |
| 19 | Eng | P2: eq://current resource = 3rd call site; add CI grep check | Mechanical | P1 completeness | Easy to miss; grep check enforces the abstraction | Trust visual review |
| 20 | Eng | P3: DenonDriver constructor must be sync; setup() in lifespan | Mechanical | P5 explicit | Async setup at module import blocks cold start when AVR is off | Sync setup in constructor |
| 21 | Eng | P3: reset_eq_state fixture must reset via driver mock | Mechanical | P1 completeness | Direct _eq_state.clear() silently stops working after refactor | Keep existing fixture |


## Failure Modes Registry

| Mode | Trigger | Impact | Mitigation |
|------|---------|--------|------------|
| Partial EQ write | MinidspApiError mid-loop in apply_eq | Hardware/state diverge; safety checks against wrong baseline | Rollback: only update _eq_state after all writes succeed |
| DenonDriver cold start fail | AVR off when server starts | lifespan startup exception | Lazy setup: setup() retry on each call; don't fail startup |
| Unknown driver config | avr_driver: yamaha (unregistered) | Server fails to start | ValueError with list of valid options |
| Concurrent apply_eq | Two MCP clients calling simultaneously | Potential double-apply | asyncio.Lock covers full read-validate-write-state |
| In-memory EQ state lost | MCP server restart | _eq_state is [] after restart | Document: server restart clears EQ memory; re-run recipe |
| config.yaml corruption | Power loss during write | All config lost, defaults loaded | Atomic write via os.replace (TODO-SP1, now required) |
| Symlink traversal | Crafted recipe name points outside recipes/ | File system read | resolve().is_relative_to() check + test |

## What Already Exists (Eng)

| Sub-problem | Existing code | Status |
|---|---|---|
| miniDSP HTTP client | calibrate/adapters/minidsp.py | Complete, well-tested; MinidspDriver wraps it |
| Denon HTTP client | denonavr library | External dep; DenonDriver wraps it |
| SafetyValidator | calibrate/safety.py | Unchanged; stays in mcp_server.py |
| Biquad conversion | calibrate/dsp.py | Unchanged; stays in mcp_server.py |
| Measurement storage | calibrate/storage.py | Unchanged |
| In-memory EQ state | mcp_server.py _eq_state dict | Moves to MinidspDriver._eq_state |

## NOT In Scope (confirmed deferred)

- Yamaha, Marantz, or any other AVR driver implementation
- CamillaDSP or other DSP driver implementation
- Auto-discovery of hardware type (SSDP MCP tool)
- MCP get_config / set_config tools
- Automated measurement loop / trigger_measurement on Pi Zero
- Recipe updates (apply_eq keeps its name)

## ## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/autoplan` | Scope & strategy | 1 | issues_open | 8 findings; 2 taste decisions surfaced at gate; user confirmed proceed; done criteria fixed; ABC narrowed; backward-compat alias added |
| CEO Voices | `/autoplan` | Dual independent review | 1 | subagent-only | Codex unavailable; Claude subagent: 7 findings (1 critical, 3 high, 3 medium); critical finding surfaced at gate |
| Design Review | skipped | No UI scope | 0 | — | — |
| Eng Review | `/autoplan` | Architecture & tests | 1 | issues_open | 11 findings (2 P0, 4 P1, 3 P2, 2 P3); all auto-decided and incorporated into plan |
| Eng Voices | `/autoplan` | Dual independent review | 1 | subagent-only | Codex unavailable; Claude subagent: 2 P0, 4 P1, 3 P2, 2 P3 |

**VERDICT:** REVIEWED [subagent-only, Codex unavailable]. Two P0s must be implemented: (1) MinidspDriver.apply_eq partial-write rollback — SafetyValidator correctness depends on this. (2) Path traversal symlink check — security hardening. All other findings auto-decided and incorporated into the plan. Plan is implementation-ready.

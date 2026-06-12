# Codebase Simplification Plan — 2026-06-12

Executor: any agent (written for step-by-step execution without deep context).
Reviewer: Andrew. Stop conditions are listed per task — when one fires, stop and report
instead of improvising.

## Context

Production setup: CamillaDSP + PipeWire + Focusrite Scarlett 18i20 on a Pi 5
(`dsp_driver: camilladsp`). The miniDSP 2x4 HD driver is a **supported alternate
driver for other users — it stays**. The retired architecture being removed here is
the **ALSA-era loopback bridge** (snd-aloop, `hw:Loopback`, loopback-ref-link), which
was replaced by the PipeWire null-sink loopback (`avr_cal_sweep` → `loopback_ref`).

## Hard rules (apply to every task)

1. **Do NOT delete or modify**: `calibrate/drivers/minidsp.py`,
   `calibrate/adapters/minidsp.py`, `deploy/99-minidsp.rules`, any miniDSP tests,
   `calibrate/safety.py` limits, `deploy/asound.conf`, `deploy/home-pi.asoundrc`
   (the last two serve the live karaoke mode, not the dead ALSA bridge).
2. **Do NOT change any PipeWire or CamillaDSP configuration on the Pi** except where a
   task explicitly lists the exact commands.
3. After every task: `uv run python -m pytest tests/ -q`. All green before commit.
   If a failure can't be fixed in 2 attempts, stop and report.
4. One commit per task, message format `refactor(scope): <task title>` (or `fix(...)`
   where the task says so). Do not push until the phase-end checkpoint says to.
5. Deploy only via `./deploy/hotfix.sh` (never manual docker commands). After any
   `ship.sh` run, verify the live image is fresh ("about a minute ago") — ship.sh has
   a known race where it reports success against the previous build.

---

## Phase 1 — Dead code removal (low risk)

### 1.1 Delete the deprecated ALSA loopback bridge

The file `deploy/loopback-ref-link.sh` is marked DEPRECATED/DISABLED in its own
header. Its replacement (PW null-sink `loopback_ref`) is live.

- Delete `deploy/loopback-ref-link.sh` and `deploy/loopback-ref-link.service`.
- In `deploy/install.sh`:
  - Remove the install block at ~lines 314–330 (installs loopback-ref-link.sh +
    service and enables it).
  - Remove the stale comment at ~lines 97–98 ("PipeWire bridges the Scarlett AUX2
    capture to snd-aloop … hw:Loopback,1,0") and at ~lines 260–261 ("Scarlett input
    ch3 → snd-aloop, bridged by the loopback-ref-link service").
  - ADD a retirement block (model it on the existing dmix-keepalive retirement at
    ~line 340) that, on upgraded Pis, runs:
    `systemctl --user disable --now loopback-ref-link.service 2>/dev/null || true`
    and removes `/usr/local/sbin/loopback-ref-link.sh` and
    `/home/pi/.config/systemd/user/loopback-ref-link.service`.
- Grep check before commit: `grep -rn "loopback-ref-link" .` must return only
  CHANGELOG/docs/git history references (none in deploy/ or calibrate/).

### 1.2 Delete `calibrate/alignment.py`

It is imported by nothing except its own test file (verified 2026-06-12:
`grep -rn "from calibrate.alignment\|from .alignment" calibrate/` → empty).

- Delete `calibrate/alignment.py` and `tests/test_alignment.py`.
- Grep check: `grep -rn "alignment" calibrate/ --include="*.py" | grep -v "sub_alignment\|optimize_sub\|time.align"` —
  review any hits; none should import the deleted module. Note:
  `optimize_sub_alignment` / `sweep_inter_sub_delay` MCP tools are unrelated
  (they live in mcp_server.py) — do not touch them.

### 1.3 Remove dead storage feature: `update_events`

CORRECTED 2026-06-12 during execution: `saved_states` is NOT dead — web.py uses
`list_states`/`save_state`/`get_state`/`delete_state` (~lines 2093–2128, the
dashboard snapshot feature). Only `update_events` has zero callers.

- In `calibrate/storage.py`: remove the `update_events` CREATE TABLE and the
  `log_update_event` / `list_update_events` methods.
- Do NOT drop tables from existing databases — removing the CREATE/usage is enough.
- Delete tests that only test the removed methods.
- Leave everything else untouched, including `saved_states`.

### 1.4 Merge the mains recipes

`recipes/core/mains-calibration.md` and `mains-calibration-v2.md` violate the
one-recipe-per-domain rule.

- Read both. The v2 file is the current methodology; confirm it covers everything
  v1 covers (if v1 has a section v2 lacks, port it into v2 and note it in the
  commit message).
- Replace the content of `mains-calibration.md` with the v2 content; delete
  `mains-calibration-v2.md`.
- Grep check: `grep -rn "mains-calibration-v2" .` → update any references
  (recipes, docs, calibrate/recipe.py listings).

### Phase 1 checkpoint

Full test suite green. `git log --oneline` shows 4 commits. Report line-count delta
(`git diff --stat main...HEAD`). Do not push yet.

---

## Phase 2 — Contradiction and footgun fixes

### 2.1 Delete `check_audio_stack_clean` (inverted preflight)

`calibrate/preflight.py` ~line 707: this check FAILS when PipeWire/WirePlumber hold
ALSA devices. It was written for the pre-PipeWire architecture; the current
architecture REQUIRES PipeWire. It only passes today because `fuser` is unavailable
inside the Docker container (the exception path returns passed=True). It is a latent
session-breaker.

- Remove the `check_audio_stack_clean` method and its entry in `run_all()`
  (the `("Audio stack", ...)` tuple).
- Delete its tests (search `tests/test_preflight.py` for `audio_stack`).
- The real coverage already exists: `check_measurement_service` (bare-metal service
  health) + `check_loopback_reference` + `check_loopback_xcorr_stability`.
- Commit as `fix(preflight): remove inverted audio-stack check (required PipeWire to be absent)`.

### 2.2 Gate minidspd launch on the configured driver

`deploy/entrypoint.sh` (~line 61) and `deploy/entrypoint-with-mcp.sh` (~line 39)
start `minidspd` unconditionally. On camilladsp-only installs this wastes a process
and log noise; miniDSP users must keep working.

- In both entrypoints, before launching minidspd, read the dsp driver from the
  mounted config: `DSP_DRIVER=$(grep -E '^dsp_driver:' /root/.avr-calibration/config.yaml 2>/dev/null | awk '{print $2}' | tr -d '"')`
  (check the actual config mount path used in the entrypoint — adjust if it differs;
  the install.sh systemd unit shows the mount).
- Launch minidspd only when `[ "${DSP_DRIVER:-minidsp}" = "minidsp" ]` — note the
  DEFAULT must remain `minidsp` so un-migrated configs keep their daemon.
- Mirror the gate in the shutdown trap (only wait for the pid if it was started).
- Test: can't run entrypoints locally against hardware — verify by shellcheck
  (`shellcheck deploy/entrypoint*.sh`, pre-existing warnings are acceptable; no NEW
  errors) and by review. After deploy, `docker logs avr-calibration` on the Pi must
  NOT show "Starting minidspd" (config there says camilladsp).

### 2.3 Resolve the 48 kHz / 96 kHz documentation conflict (VERIFY-ONLY)

`deploy/pipewire-scarlett-clock.conf` says the graph runs 48 kHz "to match
CamillaDSP", but `recipes/core/trinnov-calibration.md` Phase 4 says CamillaDSP's
processing rate is 96000. One claim is stale.

- Determine the truth from the LIVE system (read-only commands only):
  - `ssh pi@192.168.1.117 "pw-metadata -n settings | grep clock"`
  - Query CamillaDSP via the driver config: on the Pi,
    `ssh pi@192.168.1.117 "python3 -c \"import asyncio,websockets,json;
    print(asyncio.run((lambda: websockets.connect('ws://127.0.0.1:1234'))()))\""`
    is fragile — instead grep the live config the daemon was started with
    (`ssh pi@192.168.1.117 "systemctl cat camilladsp 2>/dev/null | grep -o '/[^ ]*\.yml'"`,
    then cat that file and read `samplerate:` and `capture_samplerate:`), and also
    check `~/.avr-calibration/config.yaml` `camilladsp:` block for
    `processing_rate` / `capture_samplerate`.
- Expected resolution: PW graph at 48 kHz, CamillaDSP resampling internally to a
  96 kHz processing rate (capture_samplerate=48000 + samplerate=96000), in which
  case BOTH claims are half-right. Whatever you find, update:
  - the header comment in `deploy/pipewire-scarlett-clock.conf`,
  - the rate assertion in `recipes/core/trinnov-calibration.md`,
  - add one canonical paragraph to `CLAUDE.md`'s Hardware section stating the graph
    rate, the CamillaDSP processing rate, and where each is configured.
- **STOP CONDITION**: if the live values contradict the expected resolution in a way
  that suggests a real misconfiguration (e.g. CamillaDSP processing at 48 kHz while
  FIR designs assume 96 kHz), do not change anything — report findings.

### 2.4 Migrate `cfg.minidsp.output_slots` reads to the signal graph

`calibrate/mcp_server.py` has ~10 reads of `cfg.minidsp` fields (`output_slots`
etc.) that run even on camilladsp installs, surviving only via `.get(..., [])`
guards. The signal graph (`config.signal_graph.transducers`) is the declared source
of truth for output topology.

- Add a helper in `calibrate/config.py` (or extend the existing `sub_outputs`
  property pattern at ~line 321): resolve output metadata from `signal_graph`
  first, falling back to `minidsp.output_slots` when no signal_graph is configured
  (miniDSP users typically configure output_slots, not a signal graph — the
  fallback keeps them working).
- Replace each `cfg.minidsp` read in mcp_server.py with the helper. Find them:
  `grep -n "\.minidsp" calibrate/mcp_server.py`. Leave reads inside
  minidsp-specific tool handlers (anything only reachable when
  `dsp_driver == "minidsp"`) as-is.
- Tests: existing tests must stay green; add one test that the helper prefers
  signal_graph and one that it falls back to output_slots.

### Phase 2 checkpoint

Full suite green. Push the branch, open a PR titled
`refactor: phase 1+2 simplification (dead ALSA era, preflight fixes, config hygiene)`.
Wait for CI green. **Stop and request review before Phase 3.**

---

## Phase 3 — mcp_server.py structure (do only after Phase 1+2 PR is merged)

### 3.1 Bound the in-memory caches

Module-level `_ir_cache` and `_fir_design_cache` (and `_fir_design_intent`) in
mcp_server.py grow without bound — a 64-average IR plus stacks of 24,576-tap FIR
designs accumulate for the life of the container on a Pi.

- Implement a tiny bounded dict (insertion-ordered, evict oldest beyond N=8) or use
  `collections.OrderedDict` with explicit eviction. No external deps.
- Apply to all three caches. When `apply_fir` looks up an evicted
  `design_session_id`, the existing "not found in cache" error path must fire —
  verify the error message tells the user to re-run the design tool.
- Add a unit test: insert 9 entries, assert the first is evicted.

### 3.2 Replace the if/elif dispatch with a registry

The dispatch chain at mcp_server.py ~lines 13136–13743 (~600 lines) maps tool name →
`_tool_*` call with hand-written argument unpacking, duplicating the schema
definitions earlier in the file.

Migration recipe (incremental — the chain and the registry coexist mid-migration):

1. Add near the top of mcp_server.py:
   ```python
   _TOOL_HANDLERS: dict[str, Callable] = {}

   def mcp_tool(name: str):
       def deco(fn):
           _TOOL_HANDLERS[name] = fn
           return fn
       return deco
   ```
2. In the dispatcher, BEFORE the elif chain:
   ```python
   handler = _TOOL_HANDLERS.get(name)
   if handler is not None:
       result = await handler(**{
           k: v for k, v in (arguments or {}).items()
           if k in inspect.signature(handler).parameters
       })
   elif ...  # existing chain
   ```
   Cache the signature lookup per handler (compute at registration time, store
   `(fn, accepted_params)` in the registry) — don't call `inspect.signature` per
   request.
3. Migrate tools in batches of ~10: decorate each `_tool_X` with
   `@mcp_tool("X")`, delete its elif branch. CRITICAL per batch: compare the old
   elif branch's argument handling against the function signature — some branches
   apply defaults, type coercions (`int(...)`, `tuple(...)`), or arg renames. Move
   any such coercion INTO the handler function so behavior is identical. Run the
   full suite after every batch.
4. When the chain is empty, delete it and the now-unused fallback.

- The `Tool(...)` schema list stays as-is in this task (schema generation from
  signatures is out of scope — too much behavioral risk).
- Expected savings: ~550–600 lines.

### 3.3 Evict business logic from oversized handlers

Per the architecture rule, handlers orchestrate; math lives in domain modules.

- Identify the 10 longest `_tool_*` functions:
  `awk '/^async def _tool_/{name=$3; start=NR} /^async def _tool_|^async def [^_]|^def [^_]/{if(name && NR>start) print NR-start, name; name=""}' calibrate/mcp_server.py | sort -rn | head`
  (or any equivalent method).
- For each, move contiguous pure-computation blocks (numpy/scipy work with no
  driver or storage access) into the matching domain module:
  filter/FIR math → `multi_fir.py` or `modal_fir.py`; FR/IR analysis →
  `analysis.py`; decay → `decay.py`. The handler keeps: argument validation,
  session/store loads, driver calls, response shaping.
- One commit per handler. Pure moves only — no behavior changes, no signature
  "improvements" to the moved functions beyond what the move requires.
- **STOP CONDITION**: if a block mixes computation with driver/storage calls in a
  way that can't be separated by a pure move, skip it and list it in the final
  report rather than restructuring.

### 3.4 FIR design tool consolidation

Superseded — Andrew approved consolidation 2026-06-12. Execute as **Phase 5** below
(can run before or after 3.2/3.3; if 3.2 hasn't run, "remove the tool" means
removing its elif branch too).

### Phase 3 checkpoint

Full suite green, PR per sub-task or one PR with per-task commits. Report final
line-count delta vs `main` at the start of the plan.

---

## Phase 4 — Formalize karaoke on PipeWire, retire the ALSA exclusion model

### Background (verified 2026-06-12)

Karaoke audio ALREADY flows through PipeWire: `pipewire-pulse` runs on the Pi and
Chromium appears in the live PW graph (`Chromium:output_FL → Scarlett playback_AUX0`)
— Chromium speaks the PulseAudio API, not ALSA. Consequences:

- `deploy/asound.conf` (`karaoke_out`, `vc4hdmi_8ch` plugs) and
  `deploy/home-pi.asoundrc` are bypassed; nothing in calibrate/, deploy/, or
  scripts/ references those plug names.
- The EBUSY mutual-exclusion described in those files' comments ("CamillaDSP holds
  hw:USB,0,0 → Chromium open() fails → silent in listening mode") **no longer
  functions**: PipeWire owns the ALSA device and mixes PW clients. Chromium audio
  can leak into Scarlett Line 1/2 → Denon analog → mains during listening mode.
- `audio-mode`'s `kill_kiosk` waits on `fuser /dev/snd/pcmC3D0p` — stale under PW.

The Scarlett's internal hardware mixer matrix (Mix A/B summing mics + PCM 1/2,
programmed via `amixer` in `fix-scarlett-routing.sh`) is hardware control, NOT
ALSA PCM routing — it stays regardless.

### 4.1 Verify the live karaoke path (read-only, on the Pi)

- `audio-mode set karaoke`, play something in pikaraoke, then:
  `pw-link -l | grep -i chrom` — record which Scarlett ports Chromium links to.
- Confirm sound comes out of the mains (Mix A/B → Line 1/2 → Denon).
- `audio-mode set listening` and confirm what happens to a playing kiosk stream
  (does PW keep mixing it into the Scarlett? expected: yes — this is the hole).
- **STOP CONDITION**: if karaoke audio does NOT flow via PW (no Chromium PW node
  while playing), the background assumption is wrong — report findings, change
  nothing.

### 4.2 Replace implicit EBUSY exclusion with explicit PW policy

Pick the simplest mechanism that restores the guarantee "kiosk is silent outside
karaoke mode":

- Preferred: a WirePlumber rule pinning Chromium/pikaraoke streams
  (`application.name` or `node.name` match) to a dedicated `karaoke_sink` null sink
  (created via a `context.objects` conf like 10-avr-cal-sweep.conf). `audio-mode
  set karaoke` links `karaoke_sink:monitor_FL/FR → Scarlett playback_AUX0/AUX1`;
  `set listening|cal` removes those links. Kiosk audio then dead-ends into the
  null sink outside karaoke. This matches the existing avr_cal_sweep pattern —
  same conf style, same link-script style.
- In `audio-mode`: replace the `fuser /dev/snd/pcmC3D0p` wait with the link
  add/remove; keep stopping/starting `camilladsp.service` and the pikaraoke
  container exactly as today (that part works and also handles CPU/feature
  concerns, not just device ownership).
- Update the header comments in `audio-mode` to describe the PW model.

### 4.3 Delete the dead ALSA configs

Only after 4.1 confirms and 4.2 ships and a real karaoke session works:

- Delete `deploy/asound.conf` and `deploy/home-pi.asoundrc`; remove their install
  blocks in `deploy/install.sh` (~lines 242–252) and add a retirement block that
  removes `/etc/asound.conf` and `/home/pi/.asoundrc` on upgraded Pis.
  CAUTION: before removing `/etc/asound.conf` from the Pi, confirm nothing else on
  the host uses the `vc4hdmi_8ch` plug: `grep -rn "vc4hdmi_8ch" /home/pi /etc 2>/dev/null`
  and check HDMI sweep playback still works after removal (`measure` with
  route=hdmi, or at minimum `speaker-test -D default:CARD=vc4hdmi0 -c2 -l1`).
- Update Hard rule 1 of this plan: asound.conf/home-pi.asoundrc protection no
  longer applies once this phase lands.
- Acceptance: karaoke session works end-to-end; kiosk is inaudible in listening
  mode while a video plays in the kiosk; calibration `measure` still passes
  preflight + produces coherence ≥ 0.85.

---

## Phase 5 — FIR tool consolidation (approved 2026-06-12)

### Target surface

The FIR design surface goes from 7 design tools to 4. Inventory facts verified
2026-06-12 (handler line refs from that date; re-locate with grep, they will have
drifted):

| Tool | Fate | Rationale |
|---|---|---|
| `design_fir` (~3603) | **KEEP** — absorbs `design_corrective_fir` | The single-output workhorse; referenced by 4 recipes + CLAUDE.md. |
| `design_fir_trinnov` (~4375) | **KEEP** | Multi-sub coherent Wiener; trinnov recipe. Supersedes design_fir_multi (same Wiener core in multi_fir.py; adds target phase, T60 report, xcorr gate; PR #185 history). |
| `design_modal_fir` | **KEEP** | Anti-pulse T60 cancellation — distinct physics; bass-calibration-fir + mains-v2 recipes. |
| `design_avr_fir` | **KEEP** | Audyssey/AVR hardware path — different target device. |
| `recommend_fir_phase` (~8354) | **KEEP** | Decay-based phase-mode analysis; bass-calibration-fir recipe Phase 2.5a depends on it. |
| `design_fir_multi` (~4134) | **DELETE** (tool only) | No recipe references it. design_fir_trinnov covers it: same `design_multi_input_fir` core, has `phase_mode="minimum"` for magnitude-only. |
| `design_fir_multi_modal` (~4262) | **DELETE** (tool only) | No recipe references it. The combined Wiener+anti-pulse capability remains available in code via `multi_fir.design_fir_multi_modal` / `ModalAwareFIRDesigner(base_correction=...)`. |
| `design_corrective_fir` (~7522) | **FOLD into design_fir** | Unique capability = residual correction convolved onto the FIR cached for an output (empirical 2-step). Becomes `design_fir(compose_on_output_index=...)`. |
| `apply_fir_identity` (~7694) | **DELETE** | Redundant since d287489: `clear_fir` now writes a same-length identity. Also known-buggy ("identity not passthrough" memory, 2026-06-11). |

NOT in scope (misgrouped — they are PEQ tools, not FIR): `fit_correction_filter`,
`fit_shelf_for_target`. Leave them alone.

"DELETE (tool only)" means: remove the `Tool(...)` schema entry, the `_tool_*`
handler, and the dispatch branch/registry entry. The underlying module functions in
`multi_fir.py` and their unit tests STAY — they are library code that surviving
tools and future work use.

### 5.1 Extract shared helpers (behavior-neutral, do first)

The design tools duplicate four blocks. Extract into module-level helpers in
mcp_server.py (or a new `calibrate/_tool_helpers.py` if mcp_server is being shrunk):

1. `_load_session_fr(store, session_id, require_phase=False)` → returns
   `(session, fr)` or raises a ToolError carrying the exact current error strings
   ("session {id} not found", "has no FR data", "has no phase data — re-measure
   with phase"). ~8 duplicate sites.
2. `_parse_target_curve(target_curve: dict)` → `list[tuple[float, float]]` +
   optional band. ~6 duplicate sites.
3. `_resolve_fir_rate(_dsp)` → int (driver `caps.fir_sample_rate_hz`, 48000
   fallback, with the "must match live pipeline rate" comment). ~5 sites.
4. `_cache_fir_design(session_id, output_index, taps, intent)` → cache_id, single
   place implementing the `session_id * 1000 + output_index` scheme and writing
   `_fir_design_cache` + `_fir_design_intent`.

Error-message strings must remain byte-identical where tests assert on them.
Full suite green; one commit.

### 5.2 Delete `apply_fir_identity`

- Confirm current `clear_fir` semantics first (calibrate/drivers/camilladsp.py —
  clear_fir writes identity, post-d287489). If it doesn't, STOP.
- Remove tool schema, handler (~7694), dispatch entry, and its tests (port any
  test asserting "identity preserves topology" to target clear_fir instead).
- Update `recipes/core/trinnov-calibration.md`: remove the `apply_fir_identity`
  row from the "MCP tools used" table (the recipe body already says use clear_fir).
- Grep sweep: `grep -rn "apply_fir_identity" calibrate/ recipes/ docs/ tests/` —
  zero hits outside CHANGELOG/plan docs when done.

### 5.3 Delete `design_fir_multi` and `design_fir_multi_modal` (tools only)

- Remove both schemas, handlers (~4134, ~4262), dispatch entries.
- Keep `multi_fir.design_multi_input_fir` and `multi_fir.design_fir_multi_modal`
  module functions AND `tests/test_multi_fir.py` untouched.
- Move tool-level tests: any test exercising the handlers via dispatch gets
  deleted; any asserting module behavior moves to test_multi_fir.py if not
  already covered.
- Update docs that present these as the current interface:
  - `CLAUDE.md` "FIR design — critical invariants": reword the
    `design_fir_multi regularization_lambda` and `design_fir_multi_modal`
    invariants to reference `design_fir_trinnov` / the module functions (the
    physics guidance stays — λ=0.01 for this hardware, anti-pulse-before-Wiener
    in same buffer).
  - `docs/research/trinnov-decay-correction.md`: add a one-line note that the
    standalone tools were folded into design_fir_trinnov (do not rewrite history).

### 5.4 Fold `design_corrective_fir` into `design_fir`

- FIRST write a characterization test: synthetic FR session + a cached baseline
  FIR for output N → call the existing `_tool_design_corrective_fir` → record the
  output coefficients. This test must pass before AND after the fold.
- Add to `design_fir` signature: `compose_on_output_index: int | None = None`.
  When set, after designing the residual-correction FIR, convolve it with the FIR
  currently cached for that output (port the exact convolution + fallback-to-
  impulse logic from the old handler, including the no-cached-FIR passthrough
  case). Result is cached and returned exactly like any design_fir result.
- Keep the old handler's docstring guidance (the empirical 2-step workflow) as
  part of design_fir's schema description for the new parameter.
- Delete the old tool schema/handler/dispatch entry once the characterization
  test passes against the new path.
- Grep sweep for `design_corrective_fir` in recipes/docs (its docstring mentions
  "recipe Section 2.2b" — find and update whichever recipe section that is, if it
  still exists).

### 5.5 Documentation + lessons sweep

- `grep -rn "design_fir_multi\|design_fir_multi_modal\|design_corrective_fir\|apply_fir_identity" recipes/ docs/ CLAUDE.md` —
  every remaining hit is either updated or is a historical doc (research notes,
  CHANGELOG, this plan) that explicitly may keep the old names.
- Lessons DB: call the `list_lessons` MCP tool (or query the SQLite lessons table)
  for lessons whose claim names a deleted tool. For each, call
  `invalidate_lessons` with reason "tool removed in FIR consolidation, see
  docs/plans/2026-06-12-simplification.md Phase 5". If MCP access is unavailable
  in the execution environment, list the affected lessons in the final report
  instead — do not edit the DB by hand.
- Update the tool-count assertion anywhere it appears (README/CLAUDE.md "~900
  tools" style claims — correct them to the real count while there).

### 5.6 Tool naming cleanup

Tool names are the API surface the LLM orchestrator chooses from — ambiguous names
cause wrong-tool selection (documented cost: the apply_fir_identity vs clear_fir
confusion). Procedure per rename: schema `name=`, handler reference, dispatch/registry
entry, tests, recipes, docs — one commit per rename, then
`grep -rn "<old_name>" calibrate/ recipes/ docs/ tests/ CLAUDE.md` must return only
historical docs (CHANGELOG, research notes, this plan). Lessons DB handling same as
5.5 (invalidate or report; never hand-edit).

**Definite renames:**

| Old | New | Why |
|---|---|---|
| `fit_correction_filter` | `fit_peq_for_target` | It's a PEQ optimizer (grid/LM joint fit), not FIR; parallels `fit_shelf_for_target`. |
| `resolve_target` | `resolve_measurement_target` | "target" collides with target-curve tools (`anchor_target`); this one resolves the measurement chain target ('subs'/'mains'). |

**Investigate, then rename or document (STOP and report if unclear):**

- A tool/resource registered as `Latest Measurement` (title case, spaces) appears in
  the schema list. Determine whether it's an MCP resource (acceptable) or a tool
  (rename to `get_latest_measurement` style).
- `configure_matrix` vs `set_routing`: determine whether these are duplicate
  concepts (input→output enable matrix). If one is miniDSP-specific and one
  CamillaDSP-specific, keep both but make each schema description say which driver
  it applies to. If they truly overlap, propose a merge in the final report — do
  not merge without approval.

**Deliberate keeps (do NOT rename):**

- `design_fir_trinnov`, `verify_trinnov_coherence` — jargon, but deeply embedded in
  recipes, lessons, and research docs; continuity wins.
- `clear_fir` — semantics changed in d287489 (writes identity, no longer removes
  the Conv block). Keep the name, but VERIFY the schema description and docstring
  say "resets to identity passthrough (topology preserved)" — update if stale.
- Internal-only (non-MCP) rename, free to do: preflight `check_minidsp` /
  `check_minidsp_combined` → `check_dsp` / `check_dsp_combined` (they've been
  driver-agnostic for a while; the docstring says so).

### Phase 5 checkpoint

- Tool registry/dispatch count drops by 4 (design_fir_multi, design_fir_multi_modal,
  design_corrective_fir, apply_fir_identity).
- Full suite green; characterization test from 5.4 in the suite permanently.
- Recipes reference only surviving tools.
- One PR titled `refactor(fir): consolidate FIR tool surface 7→4 design tools`.
- **STOP CONDITION** (whole phase): if at any point a surviving tool turns out NOT
  to cover a deleted tool's capability (e.g. design_fir_trinnov lacks a
  design_fir_multi parameter someone needs), stop and report rather than porting
  parameters ad hoc.

---

## Out of scope (do not touch)

- `safety.py` limits and validator logic.
- The bare-metal measurement service architecture (`measurement_service.py` /
  `measurement_client.py` split).
- `web.py` (self-contained read-only dashboard).
- Anything that changes measurement semantics: deconvolution, loopback reference
  handling, xcorr windows (recently fixed in d287489 — keep hands off).
- PipeWire link scripts / WirePlumber rules (fixed in d287489).

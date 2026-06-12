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

### 1.3 Remove dead storage features: `update_events` and `saved_states`

Verified 2026-06-12: `log_update_event`, `list_update_events`, `save_state` (the
saved_states one at storage.py ~line 798, NOT `save_calibration_*`), and the
saved_states list/get/delete methods have zero callers in calibrate/, deploy/,
scripts/, or cli.py.

- In `calibrate/storage.py`: remove the `update_events` (~line 159) and
  `saved_states` (~line 202) CREATE TABLE statements and all methods that
  reference those two tables (`log_update_event` ~530, `list_update_events` ~550,
  `save_state` ~798, and the saved_states SELECT/DELETE methods ~830–865).
- Do NOT drop tables from existing databases — removing the CREATE/usage is enough;
  old DBs keep orphan tables harmlessly.
- Re-verify zero callers before deleting each method:
  `grep -rn "<method_name>" calibrate/ deploy/ scripts/ tests/`.
  Delete any tests that only test the removed methods.
- Leave `feedback`, `equipment`, `sessions`, `calibration_runs`,
  `calibration_iterations`, `active_dsp_state*`, `lessons*` untouched.

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

### 3.4 FIR design tool consolidation — PROPOSAL ONLY

Do NOT implement. Produce a one-page proposal (`docs/plans/fir-tool-consolidation.md`)
mapping the ten FIR-related tools (`design_fir`, `design_fir_multi`,
`design_fir_multi_modal`, `design_modal_fir`, `design_fir_trinnov`,
`design_corrective_fir`, `design_avr_fir`, `fit_correction_filter`,
`fit_shelf_for_target`, `recommend_fir_phase`) onto a target surface of ~3, with the
shared helpers (target-curve parsing, measurement loading, rate checks) they'd use.
Identify which recipes/memories reference each tool name. Andrew decides before any
tool is removed — recipes and saved lessons reference these names.

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

## Out of scope (do not touch)

- `safety.py` limits and validator logic.
- The bare-metal measurement service architecture (`measurement_service.py` /
  `measurement_client.py` split).
- `web.py` (self-contained read-only dashboard).
- Anything that changes measurement semantics: deconvolution, loopback reference
  handling, xcorr windows (recently fixed in d287489 — keep hands off).
- PipeWire link scripts / WirePlumber rules (fixed in d287489).

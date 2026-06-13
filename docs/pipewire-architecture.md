# PipeWire & Measurement-Capture Architecture — Design Spec + Codebase Audit

> Status: authoritative design reference. Written 2026-06-13 after a session that
> burned hours on recurring PipeWire/measurement failures (loopback contamination,
> flat-FR, "sweep not detected", non-deterministic xcorr, sign flapping). The
> failures were all symptoms of the same root cause; we kept patching symptoms.
> This document defines how the stack **should** work from first principles, with
> reasoning, and then audits the codebase against it.

---

## 0. Why this document exists

Across this session we applied at least six independent "band-aids" to keep audio
working: pin null-sink suspend, `node.dont-reconnect`, `restore-target=false`,
make `audio-mode` own the playback links, revert a `PIPEWIRE_LATENCY` experiment,
unmute a stale output. Each fixed a symptom; none addressed the cause. A
band-aid count that high is itself the diagnosis: **we are fighting the tool.**

The remedy is not another patch. It is a written model of the intended behavior
that every config and code path can be checked against. Anything that violates a
rule below is a bug, even if "it happens to work today."

---

## 1. First principle — this is a fixed appliance, not a desktop

PipeWire ships with **WirePlumber**, a *session/policy manager* designed for
general-purpose desktops: applications come and go, devices hot-plug, and the
"right" routing is a moving target. WirePlumber's whole job is to **make dynamic
routing decisions on your behalf** — autoconnect new streams to a sensible
target, fall back to "the default source/sink" when a target is missing, remember
where a stream last went and restore it, reconnect after failures.

This rig is the **opposite** of that world:

- The device set is fixed (Scarlett 18i20, UMIK, two null sinks).
- The routing is fixed and known ahead of time.
- The graph topology never legitimately changes at runtime.

Every dynamic decision WirePlumber makes for us is therefore, at best, redundant
and, at worst, wrong. **Every chronic failure this session traces to a WP dynamic
decision applied to a graph that did not want one:**

| Symptom | WP dynamic behavior that caused it |
|---|---|
| UMIK auto-linked into `camilladsp_capture` (mic→subs→room→mic feedback loop) | autoconnect fallback to "default source" |
| 20 `camilladsp_playback` outputs linked into `loopback_ref` (reference contamination → flat-FR) | fallback target search picked the null-sink |
| mic and ref `pw-record` returned identical data | restore-target remembered a target across `pw-record`s |
| WP link policy "wedged permanently" after a client died mid-activation | policy-node lifecycle |
| node names changed to `pro-output-0` | WP re-applied a stale card profile |

**The governing principle:**

> **WirePlumber is allowed to do device *enumeration* only — create the ALSA
> nodes, set the card profile and clock. It must make *zero* routing decisions.
> All links are created explicitly by exactly one owner. Capture and playback
> always target an explicit, named node — never a "default" and never a generic
> portal.**

Everything below is a consequence of that principle.

---

## 2. The intended graph

### Devices (created/enumerated by WirePlumber, ACP multichannel profile)

- `alsa_output.…Scarlett…multichannel-output` — 20-ch sink (AUX0…AUX19) → physical line outs → subs/shaker.
- `alsa_input.…Scarlett…multichannel-input` — 20-ch source (AUX0…AUX19); AUX2 = Denon LFE pre-out.
- `alsa_input.…Umik-1…analog-stereo` — the measurement microphone (its own USB clock).

### Static null sinks (created declaratively by `pipewire.conf.d` drop-ins, pinned hot)

- `avr_cal_sweep` — stereo null sink. Calibration sweep is played **into** it; its
  monitor is the clean, pre-DSP electrical copy of the stimulus.
- `loopback_ref` — mono null sink. Receives the sweep monitor; the measurement
  records **from** it as the deconvolution reference.

### CamillaDSP (native PipeWire client, both nodes `autoconnect=false`)

- `camilladsp_capture` (input_1…input_20) — input_3 (channel index 2) is the LFE feed.
- `camilladsp_playback` (output_1…output_20) — → Scarlett AUX0…AUX19.

### The four signal paths

```
LISTENING (steady state):
  Denon LFE pre-out → Scarlett:capture_AUX2 → camilladsp_capture:input_3
       → CamillaDSP (xover/EQ/FIR/gain) → camilladsp_playback:output_{1..20}
       → Scarlett AUX → subs

CAL — stimulus to the subs:
  pw-cat --target avr_cal_sweep  →  avr_cal_sweep:monitor_FL
       → camilladsp_capture:input_3  →  CamillaDSP  →  Scarlett AUX → subs
       (input_3 is LOAD-BEARING; removing it silences the subs)

CAL — deconvolution reference X (pre-DSP electrical copy of the stimulus):
  avr_cal_sweep:monitor_FL  →  loopback_ref:playback_1  →  pw-record(loopback_ref)

CAL — measured signal Y (acoustic room response):
  UMIK (room)  →  pw-record(alsa_input.…Umik…)   [through PW, resampled to graph clock]

Deconvolution:  H(f) = Y(f) / X(f)
```

The single explicit wiring owner is **`audio-mode wire`** (invoked from
`avr-cal-sweep-link.service`, `camilladsp.service` ExecStartPost, and the
watchdog). It creates: the two CAL links above, the 20 playback→Scarlett links,
and the listening LFE link; and it removes any stray UMIK→capture link.

---

## 3. Design rules (with reasoning)

Each rule states the invariant, the reasoning, and the concrete failure it
prevents (with this session's evidence where applicable).

### R1 — WirePlumber makes zero routing decisions
Device enumeration + profile/clock only. *Reason:* §1. *Prevents:* the entire
"WP linked something we didn't ask for" failure class.

### R2 — `autoconnect = false` on every node
`camilladsp_capture`, `camilladsp_playback`, the UMIK, and both null sinks.
*Reason:* a node with `autoconnect=true` invites WP to pick a target; when it
can't reach the intended one, WP's fallback links it to *any* sink. *Prevents:*
the UMIK→capture feedback loop and the `camilladsp_playback`→`loopback_ref`
contamination (which manifested as **"suspiciously flat FR"** measurement
failures, std ≈ 1.3 dB). This is one rule that subsumes several band-aids: with
nothing autoconnecting, there is no fallback to guard against.

### R3 — `audio-mode wire` is the sole link owner
All links created in exactly one place; idempotent; re-run on every disruption.
*Reason:* two owners (WP + a script) race and contradict each other. *Prevents:*
playback links being created twice (once by CamillaDSP autoconnect, once by the
script) and the resulting non-determinism.

### R4 — Every capture/playback targets an explicit named node
Never a "default source/sink"; never a generic portal device (PortAudio's
`pipewire`/`default`/`pulse`). *Reason:* "default" is a runtime WP choice that
changes across reboots; a generic portal captures *whatever WP currently routes
to it* — often the default **sink monitor** (which carries the stimulus), not the
source you wanted. *Prevents:* the mic recording the sweep reference instead of
the room → flat FR. Evidence: with `mic.name="pipewire"` the "mic" resolved to
the 64-ch `pipewire` portal (device 7) and produced a flat deconvolution even
though the UMIK was the default source.

### R5 — One graph clock (48000 / 256), pinned globally; never force a per-stream quantum
Pinned in `50-scarlett.lua` (`clock.*-quantum = 256`, rate 48000). *Reason:* a
single quantum grid means every client (CamillaDSP, the sweep player, both
recorders) is scheduled on the same boundaries. Forcing a per-stream
`PIPEWIRE_LATENCY`/`force-quantum` triggers a **graph renegotiation**, during
which `pw-record` can rebind to the wrong node. *Prevents:* the regression we
caused and reverted on 2026-06-13 (adding `PIPEWIRE_LATENCY=256/48000` to the
measurement service → flat-FR + ±15 dB magnitude swings).

### R6 — The UMIK's independent clock is bridged ONLY by capturing it *through* PipeWire — and this is what fixes HF coherence
The UMIK has its own USB crystal, asynchronous to the graph clock. Two
free-running ~48 kHz crystals differ by a fractional rate ε (tens–hundreds of
ppm). Over a sweep of duration *t* the phase error is **Δφ(f) = 2π·f·ε·t — it
grows with BOTH frequency and elapsed time.** That is precisely a coherence loss
that is *worst at high frequency and worst late in the sweep*.

Capturing the UMIK **through PW** (`pw-record` on the UMIK node) routes it through
PW's adaptive resampler (`resample.quality = 14`, `51-umik.lua`). *Reason:* the
resampler's **rate-tracking loop** continuously estimates the UMIK/graph rate
ratio and drives ε → 0 in the recorded samples — the samples land on the graph
timebase. (Note: the win is the rate-tracking, **not** the sinc tap count — do
not chase coherence by adding taps.) Capturing the UMIK **directly** (PortAudio
`hw:4,0`) bypasses that loop → ε persists → HF coherence falls off.

**Crucially, this cannot be fixed downstream:** clock drift is a time-axis
*dilation* `y(t·(1+ε))`, not a constant lag, so the loopback cross-correlation
(which finds a single best delay) **cannot** remove it. The only fixes are to
bridge the clock *at capture* (this rule) or to resample Y by the estimated ratio
before deconvolution (not implemented; don't). *Prevents:* the
declining-with-frequency coherence (0.92 @ 40 Hz → 0.53 @ 160 Hz at ~39 dB SNR)
seen when the mic was captured off the resampler path. Documented earlier:
quality 4 → coh ≈ 0.72; quality 14 → coh ≈ 0.99.

> **R6 owns high-frequency coherence. R7 (below) owns xcorr stability. They are
> different failure modes — do not credit one with fixing the other.** A run that
> reports "coherence improved" after an R7 change has proven nothing about R6;
> verify them separately (see §6 verification).

### R7 — Reference and mic are captured the same way and started synchronously
Both via `pw-record` (R6), and both recordings must share a common t=0 — **start
them together** (preferred), or deterministically trim the known offset between
them (fragile fallback). *Reason:* the deconvolution `H = Y/X` assumes X and Y
share both a clock (R5/R6) **and** a timebase. The ref/mic start offset here is a
**per-run constant** (set once before the sweep): a constant offset is a *pure
delay* — magnitude-flat and **frequency-flat in its coherence effect** — and a
*fixed* value is found and removed by the loopback cross-correlation. What R7
fixes is the **run-to-run variation** of that offset: when it varies,
`loopback_xcorr_peak_ms` varies run-to-run, so **sessions can't be phase-compared
for Trinnov** (the design needs all subs measured against a consistent reference
timebase). *Prevents:* two identical back-to-back sub5 sweeps producing
`xcorr_peak_ms` 7.48 vs 12.98 ms and `avr_processing_ms` 650 vs 858 ms, because
the ref `pw-record` starts, then a variable binding-verification delay elapses,
then the mic capture starts.

> **R7 does NOT fix high-frequency coherence decline** — a constant per-run delay
> does not preferentially decorrelate high frequencies (that's R6/clock drift).
> R7 buys *stable, phase-comparable* sessions. Do not expect coherence to improve
> from R7 alone.

*Implementation cautions (from review):* (1) the deterministic-trim must be
**clamped** — never trim past the first ref sample above the noise floor, or it
chops the IR onset and reads the response low (a confident-but-wrong number).
Wall-clock `t_ref_start`/`t_base_start` bracket *thread scheduling*, not the
instant PipeWire delivered the first sample, so they cannot resolve sub-sample
alignment; that residual is a constant delay (harmless, absorbed by xcorr).
(2) Synchronous start is strictly better than estimate-and-subtract. (3) The
synchronous-start path MUST inherit the pinned 48000/256 graph quantum and
request no per-stream latency — otherwise it triggers a graph renegotiation
(the exact R5 failure we reverted). (4) Verify the **mic** `pw-record` binding
too — today only the ref is verified, and the documented failure is both streams
falling back to the *same* default node (the ref/mic identity guard exists
because that happened).

### R8 — Null sinks are pinned hot (never idle-suspend)
`session.suspend-timeout-seconds = 0` + `node.pause-on-idle = false` on
`avr_cal_sweep` and `loopback_ref`. *Reason:* a suspended null sink stops passing
audio to its monitor; on resume it can come back at a different latency. *Prevents:*
the "first sweep after idle is wrong / warmup" class and quantum shifts on resume.

### R9 — A state reset clears ALL per-output state, including mute
`mute` is a separate flag from `gain_db`. *Reason:* a reset that only zeros gain
leaves a stale mute in place. *Prevents:* this session's "sweep not detected" —
`start_calibration` reported `gain_reset: true` on output 5 but left it
`mute=True` from the prior run, so the only routed sub was silent.

### R10 — Belt-and-suspenders while WirePlumber is running
`restore-target = false` (`41-no-restore-target.lua`) and
`node.dont-reconnect = true` on `loopback_ref` (`11-loopback-ref.conf`). *Reason:*
even with R2, these close the remaining WP avenues (target memory across
identically-named `pw-record`s; fallback reconnection). Cheap insurance; keep
them until/unless WP policy is disabled wholesale.

---

## 4. The deconvolution model (why R5–R7 matter, precisely)

The measurement computes `H(f) = Y(f) / X(f)` where:

- `X` = `loopback_ref` recording = clean pre-DSP stimulus.
- `Y` = UMIK recording = acoustic room response.

For `H` to be the room transfer function, X and Y must agree on **two** things:

1. **Clock** — samples must advance at the same rate. The Scarlett/graph runs the
   pinned 48000/256 clock (R5). The UMIK runs its own clock; capturing it through
   PW resamples it onto the graph clock (R6). If they don't share a clock, the
   phase error grows linearly with elapsed time → high-frequency decorrelation.

2. **Timebase (t=0)** — the two recordings must be alignable to a common origin.
   A *constant* lead/lag between X and Y is found and removed by the loopback
   cross-correlation (`loopback_xcorr_peak_ms`). A *variable* lead/lag (different
   on every sweep) cannot be calibrated out and shows up as run-to-run xcorr
   variance and a degraded coherence proxy (R7).

"Coherence" here is an **IR-tail-SNR proxy**, not textbook magnitude-squared
coherence; it is sensitive to X/Y misalignment as well as to acoustic SNR. So a
clean chain with good SNR can still report mediocre coherence purely from an R7
timebase violation — which is exactly what we observed.

---

## 5. Codebase audit — actual vs. intended

| Rule | Status | Evidence / notes |
|---|---|---|
| R1 WP enumeration-only | ✅ after fix | `audio-mode` owns all links; WP no longer creates routing for our nodes. |
| R2 autoconnect=false everywhere | ✅ after fix | Was violated: `_DEFAULT_PLAYBACK_DEVICE.autoconnect_to = Scarlett` (capture was already null). Fixed 2026-06-13 → both `None` in `calibrate/drivers/camilladsp.py`; live `scarlett.yml` + `config.yaml` patched; regression test in `tests/test_drivers.py`. |
| R3 single wiring owner | ✅ | `deploy/audio-mode` `cmd_wire`; called from service + watchdog. |
| R4 explicit named-node targets | ❌ **open** | Mic capture has **two tangled mechanisms**: (a) `pw-record` on `capture_pipewire_node = mic_pipewire_node` (correct, explicit UMIK node — logs show `cap=…Umik…`), AND (b) a PortAudio `sd.default.device` set from `cfg.mic.name` via `_find_umik_device`. `mic.name="pipewire"` resolves (b) to the generic portal. Changing `mic.name` changed flat→working **even though logs showed `pw-record cap=UMIK` both times** — i.e. the two paths interact in a way that is not predictable from the code. A single deterministic path is required. |
| R5 one pinned clock; no per-stream quantum | ✅ (after revert) | `50-scarlett.lua` pins 48000/256. `PIPEWIRE_LATENCY` experiment added then reverted same day; documented as forbidden. |
| R6 UMIK bridged through PW resampler | ⚠️ partial | The active sweep path *does* `pw-record` the UMIK node (good) with `resample.quality=14` present. But the alternate PortAudio path (R4-b) and the `mic.name="Umik"` direct-`hw:` capture bypass the resampler → declining coherence. The direct path must not exist for sweeps. |
| R7 ref+mic captured identically & synchronously | ❌ **open** | `LoopbackRefPlayback.play_and_record` starts the ref `pw-record`, runs `_verify_pw_record_binding` (variable 100–300 ms+), *then* starts the base thread that opens the mic capture. No synchronization, no offset compensation, and **no binding verification on the mic** (only the ref). Evidence: xcorr 7.48 vs 12.98 ms on identical sweeps. |
| R8 null sinks pinned | ✅ | `10-avr-cal-sweep.conf`, `11-loopback-ref.conf`. |
| R9 reset clears mute | ❌ **open** | `start_calibration` reset zeroes gain/polarity/delay/FIR/EQ but not the per-output `mute` flag; stale `mute=True` on output 5 caused "sweep not detected" until manually unmuted. |
| R10 WP guards | ✅ | `41-no-restore-target.lua`, `dont-reconnect=true`. |

### The pattern in the open items (R4, R6-partial, R7, R9)

All four are the **same class as the WP problems**: a piece of state or behavior
that is *implicit / dynamic / non-deterministic* where the appliance needs
*explicit / static / deterministic*. R4 trusts a "default" device; R7 trusts
loose thread timing; R9 trusts that "reset" cleared everything. The fixes are the
same shape as the WP fixes: make it explicit and single-path.

---

## 6. Recommended remediations (priority order)

1. **R4/R6 — one deterministic mic path *through the resampler* (the coherence
   fix).** This is what restores high-frequency coherence. For the sweep route,
   capture the mic **only** via `pw-record` on the explicit UMIK node
   (`mic_pipewire_node`) so it always passes through PW's rate-tracking resampler.
   Remove the PortAudio `sd.default.device` capture path for sweeps (or make
   `mic.name` strictly select the `pw-record` node, never a generic portal). Delete
   `mic.name="pipewire"` from config; the portal must never be a capture target.
   Add a preflight assertion that the resolved mic capture is the UMIK PW node (a
   *routing* check) **and** a clock-drift sanity check (a *different* guarantee —
   e.g. assert the path is the PW node, not `hw:`/portal, before trusting any
   phase/coherence). NB: the recent logs showed `cap=…Umik…analog-stereo`
   (through-PW) yet HF coherence still declined, so first **confirm which capture
   actually feeds the deconvolution** — the dual path means the through-PW `cap=`
   log and the PortAudio `sd.default` mic can coexist; the one used for `Y` must be
   the resampled PW node.

2. **R7 — synchronize ref + mic capture (the xcorr-stability fix).** In
   `LoopbackRefPlayback`, start both `pw-record`s before the stimulus (binding
   verification on *both*, concurrently/after), so the stimulus onset is the common
   t=0. Prefer this over estimate-and-subtract; if a trim is kept, **clamp it** to
   never pass the noise-floor onset, and ensure both `pw-record`s inherit the
   pinned 48000/256 quantum (no per-stream latency → no graph renegotiation, R5).
   Expected effect: `loopback_xcorr_peak_ms` variance collapses and sessions become
   phase-comparable — **not** an HF-coherence change (that's item 1).

3. **R9 — reset clears mute.** `start_calibration`'s reset path must set
   `mute=False` (and any other boolean flags) alongside the numeric resets, and
   `check_system` should flag any routed output left muted at session start.

4. **(done) R2 — playback `autoconnect_to: null`** in code, live config, and
   `config.yaml`; regression-guarded.

5. **Keep R10 guards.** Do not remove `restore-target=false` /
   `dont-reconnect=true` while WP policy is still running.

### Verify R6 and R7 *separately* (don't let one take credit for the other)

After implementing, prove each fix with the metric it actually moves — the same
discipline as "verify FIR via tap analysis, not a room A/B":

- **R7 proof:** repeat the *same* sweep N times; show `loopback_xcorr_peak_ms`
  variance collapses (e.g. from ~5 ms range to < 0.5 ms). This says nothing about
  coherence.
- **R6 proof:** show coherence at 160 Hz recovers, and that it recovers **only
  when the mic is captured through the PW resampler** (not direct `hw:`). A single
  "coherence went up" reading must not be allowed to credit R7.

A clock-drift sanity check belongs in preflight: assert the mic capture feeding
the deconvolution is the UMIK *PW node* (resampled), not a `hw:` device or a
portal — a routing assertion is necessary but not sufficient; the point is the
clock is bridged.

### Optional larger step

If WP fallback keeps finding new ways to misbehave, the principled end-state is to
**disable WirePlumber's policy/linking component entirely** (run it for device
enumeration only) so the appliance's static, explicitly-owned wiring is the only
thing that ever creates a link. R1–R3 already assume this in spirit; making it
literal removes the last reason to keep R10's belt-and-suspenders.

---

## 6b. The sweep-capture deep-dive — the two-stream offset flaw (root cause)

After R2/R7/R9 the chain *still* degraded: variable `xcorr_peak_ms`, 494 ms group
delays, coherence collapsing and getting worse over the session, with occasional
0.99 "it works!" runs. Re-examining the actual sweep-capture path against the
ideal exposed a flaw deeper than R7.

**The deconvolution assumes ref and mic are sample-aligned. The capture path does
not provide that.**

- `_compute_fr_arrays` (`measurement.py`) computes `H = Y·conj(X)/(|X|²+ε)` with
  `X` = loopback-ref recording and `Y` = mic recording, **FFT'd as-is, with no
  alignment between them**. Any relative start-time offset between the two
  recordings is baked into `H`'s phase → the IR peak lands at that offset.
- The IR gate takes `ir_full[:gate_samples]` from **t=0** (not from the IR peak),
  and the coherence proxy compares `ir_full[:gate]` (signal) vs
  `ir_full[gate:2·gate]` (noise). If the offset pushes the IR peak toward/past the
  gate boundary, the signal is clipped and the noise window overlaps real signal →
  magnitude garbage + coherence collapse.
- `X` and `Y` are captured by **two independent `pw-record` streams** with
  independent start times (ref in `LoopbackRefPlayback`; mic inside the base
  strategy). The USB sub path applies `warmup_n = 0` trim — i.e. **no alignment at
  all**. The relative offset is therefore whatever the two subprocesses' bind/start
  scheduling produced that run: small (→ good) or hundreds of ms (→ garbage),
  non-deterministically.

This single mechanism explains the whole symptom set: variable xcorr, huge group
delays, coherence chaos, session-long degradation, and the *lucky* 0.99 runs (the
two streams happened to start close together). 

**Why the obvious fixes don't work:**
- **R7 (concurrent start)** only *reduces* the offset — two separate subprocess
  streams still bind/start on different graph cycles. Necessary, not sufficient.
- **Peak-aligning each IR before gating** (roll so argmax → t=0) fixes magnitude
  and coherence but **destroys inter-sub phase**, which Trinnov requires. Not an
  option. The loopback ref exists precisely to carry a *common* timing reference
  across subs; per-measurement peak-alignment throws that away.

**The ideal: one sample-locked capture stream.** Capture the reference and the mic
in a **single `pw-record` with 2 channels** — ch0 ← `loopback_ref` monitor, ch1 ←
UMIK (e.g. link both into one 2-ch null sink and record that once). One stream =
one clock, one start instant = **zero relative offset, deterministically, every
time**. Then `H`'s phase reflects only the acoustic+DSP path, the IR sits at its
true (small) delay inside the gate, coherence stops depending on luck, and
inter-sub phase is preserved for Trinnov. This supersedes R7's "synchronize the
two streams" with "there is only one stream."

> **R11 (new): the reference and the mic are ONE capture, not two.** Two
> independently-started recordings cannot be relied on to share a timebase; a
> single multi-channel capture is sample-locked by construction. This is the
> structural fix for the timebase-offset class (R7 was the partial, two-stream
> mitigation).

Implementation note: this is a measurement-engine change (capture both channels in
one `pw-record`, deconvolve ch1/ch0). It must keep R5 (inherit the pinned
48000/256 quantum) and R6 (the UMIK channel still rides PW's resampler onto the
graph clock). Verify with the §6 discipline: same-sweep xcorr variance → ~0, and
coherence no longer luck-dependent across a rapid N-sweep burst.

## 7. One-line invariant (put this on the wall)

> **Fixed appliance → static explicit wiring. WirePlumber enumerates devices and
> nothing else. Every link, every capture, every playback names an explicit node.
> The reference and the mic are captured the same way, through PipeWire, started
> together. Resets clear everything, including mute.**

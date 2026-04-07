# miniDSP 2x4 HD — PEQ Biquad Write Hang Debug Log

## Problem Statement

Writing real IIR biquad coefficients to the miniDSP 2x4 HD causes the DSP to hang:
- Output level meters freeze at **0.0 dBFS** (should be -100 to -120 dBFS at idle)
- Audio stops
- Bypassing slots, switching presets, and restarting the container **do not recover it**
- Only a **physical power-cycle** of the miniDSP unit recovers it

## Hardware Context

- Unit: miniDSP 2x4 HD (specific unit, possibly partially defective)
- Output 0: **physically defective** — hangs on any PEQ write regardless of conditions
- Outputs 1, 2: used for subs (SVS PB12-NSD)
- Output 3: unused
- Container: `avr-calibration` on Pi 5 @ 192.168.1.117
- CLI: `minidsp` (minidsp-rs, WebSocket transport to minidspd)

## Coefficient Reference

All at 96kHz sample rate (miniDSP 2x4 HD internal rate).

### Identity (passthrough)
```
b0=1.0  b1=0.0  b2=0.0  a1=0.0  a2=0.0
```
→ No IIR feedback. Safe baseline.

### HPF 18Hz — 4th-order Butterworth, Section 0
```
b0=0.9984619257167581
b1=-1.9969238514335161
b2=0.9984619257167581   ← symmetric: b0=b2
a1=-1.9978241409742663  ← near -2 (near stability boundary)
a2=0.9978255273782353   ← near +1
```
Poles: |z| = 0.9989 (stable). CLI command:
```
minidsp output 1 peq 2 set -- 0.9984619257167581 -1.9969238514335161 0.9984619257167581 -1.9978241409742663 0.9978255273782353
```

### HPF 18Hz — 4th-order Butterworth, Section 1
```
b0=1.0  b1=-2.0  b2=1.0  a1=-1.9990973426531968  a2=0.999098729940713
```
Note: `b1=-2.0` exactly. We only write Section 0 (one biquad per filter slot).

### Peaking 80Hz, -3dB, Q=0.707
```
b0=0.9987203134966584
b1=-1.9912093556606645
b2=0.9925163375433332
a1=-1.9912093556606645   ← same as b1 (peaking filter property)
a2=0.9912366510399917
```
CLI command:
```
minidsp output 1 peq 2 set -- 0.9987203134966584 -1.9912093556606645 0.9925163375433332 -1.9912093556606645 0.9912366510399917
```

### Peaking 40Hz, -3dB, Q=1.0
```
b0=0.9995463441785419
b1=-1.996886501916827
b2=0.9973470009799832
a1=-1.996886501916827
a2=0.9968933451585252
```

## History of Fix Attempts

| Date | Commit | Approach | Result |
|------|--------|----------|--------|
| Apr 6 | `3072c9c` | Per-output gain mute before PEQ write (HTTP batch) | Output 0 still hung (DSP still processes on gain-muted outputs) |
| Apr 6 | `29b51e6` | Master mute before PEQ write (HTTP batch) | Output 0 still hung ("regardless of mute state") |
| Apr 6 | `327964a` | Revert mute workarounds — declared output 0 as hardware defect | Outputs 1-3 "fine" — but NOT tested with real biquad coefficients! |
| Apr 7 | `82d0210` | Switch from HTTP batch to CLI (WebSocket transport) | Only tested with **identity filter** (a1=0, a2=0). Never tested real IIR coefficients. |
| Apr 7 | — | First real CLI write: HPF 18Hz to outputs 1 and 2 | **HANG** — outputs 1 and 2 frozen at 0.0 dBFS |
| Apr 7 | `929f9cd` | Master-mute via CLI (`minidsp mute on`) before writing active biquad slots | **Pending validation after physical reset** |

## Test Matrix

Status legend: ✅ = confirmed OK, ❌ = confirmed HANG, ❓ = untested

### Key Finding — Session 2

**`peq set` is safe. `bypass off` is the trigger.**

The verbose log shows two distinct commands:
- `WriteBiquad` (from `peq set`) — writes coefficients to slot memory. Safe. Output levels unchanged.
- `WriteBiquadBypass { value: false }` (from `bypass off`) — ACTIVATES the filter. **This triggers the hang.**

The DSP is always "active" at idle (~-94 dBFS noise floor from analog inputs). Activating any real IIR biquad slot on a running DSP causes the freeze. The "no signal" assumption was incorrect.

**Implication:** Only the `bypass off` call needs protection. `peq set` can be called freely.

### Transport × Filter Type × Mute State

| Transport | Command | Filter | Mute? | Output 1 | Notes |
|-----------|---------|--------|-------|----------|-------|
| HTTP batch | batch | Identity | None | ✅ | |
| HTTP batch | batch | Peaking 80Hz | None | ❌ | |
| CLI | `peq set` | Peaking 80Hz | None | ✅ | **Set alone is safe** |
| CLI | `peq bypass off` | Peaking 80Hz | None | ❌ | **Bypass off triggers hang** |
| CLI | `peq bypass off` | HPF 18Hz | None | ❌ | Same hang (not HPF-specific) |
| CLI | `peq bypass off` | Peaking 80Hz | Master mute | ❌ | Mute does NOT stop DSP internally |
| CLI | `peq set` (to active slot) | Peaking 80Hz | None | ❌ | Active slot overwrite also hangs |
| CLI | `peq bypass off` | Peaking 80Hz (NEGATED a1/a2) | None | ❓ | Pending — core theory test |
| CLI | `peq set` (to active slot) | Peaking 80Hz (NEGATED a1/a2) | None | ❓ | Pending |

## Verbose Debug Procedure

After physical reset, use `-v` flag to capture exactly what the CLI sends to hardware.

### Step 1 — Verify recovery (meters at idle)
```bash
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status"
# output_levels should be [-120, -X, -X, -120] NOT [_, 0.0, 0.0, _]
```

### Step 2 — Test identity filter (baseline, should not hang)
```bash
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 set -- 1.0 0.0 0.0 0.0 0.0 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 bypass off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status 2>&1" | grep output_levels
```
Expected: output_levels unchanged (not frozen).

### Step 3 — Test peaking filter WITHOUT mute (does CLI hang for peaking?)
```bash
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 set -- 0.9987203134966584 -1.9912093556606645 0.9925163375433332 -1.9912093556606645 0.9912366510399917 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 bypass off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status 2>&1" | grep output_levels
```
Expected: ✅ if CLI doesn't hang for peaking, ❌ if it does.

### Step 4 — Test HPF WITHOUT mute (reproduce the original hang)
> ⚠️ This WILL hang if our theory is correct. Have power-cycle plan ready.
```bash
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 set -- 0.9984619257167581 -1.9969238514335161 0.9984619257167581 -1.9978241409742663 0.9978255273782353 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 bypass off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status 2>&1" | grep output_levels
```

### Step 5 — After reset, test HPF WITH master mute (validate the fix)
```bash
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v mute on 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 set -- 0.9984619257167581 -1.9969238514335161 0.9984619257167581 -1.9978241409742663 0.9978255273782353 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 bypass off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v mute off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status 2>&1" | grep output_levels
```
Expected: ✅ output levels NOT frozen (validates the fix).

## Findings Log

### Session 1 — 2026-04-07

**Setup:** First real calibration run using `harman-bass-persub` recipe.

**What was submitted:**
- Phase 0: level calibration — gain/delay/polarity writes only (no PEQ)
- Phase 1: alignment — `set_delay(1, 15.85ms)`, `set_polarity(1, inverted=True)`, `set_output_gain(1, +2.7dB)`
- Phase 2: per-sub EQ — both subs flat in working range, only 18Hz HPF needed
  - `apply_eq([{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}])` → targets=[1,2]

**Result:** After writing HPF section 0 to output 1 (via CLI, `peq 2 set -- ...`), both outputs 1 and 2 froze at **0.0 dBFS**. Device required physical reset.

**Diagnosis:** First real IIR biquad write of the session. CLI `peq set` with a1≈-2 causes hang — same hardware issue as HTTP batch write. The commit 82d0210 fix was incomplete (only validated with identity filter a1=0).

**Fix applied:** `929f9cd` — master-mute before active biquad writes + post-write hang detection.

**Status:** Pending validation (device needs physical reset).

---

### Session 2 — 2026-04-07

**Setup:** Physical reset completed. Device at idle (~-94 dBFS output levels from analog noise floor).

**Test: Peaking 80Hz -3dB Q=0.707 on output 1, no mute**

```
minidsp -v output 1 peq 2 set -- 0.9987... -1.9912... 0.9925... -1.9912... 0.9912...
```
Verbose output showed: `WriteBiquad { addr: PEQ_4_8, data: [0.9987203, -1.9912094, 0.99251634, -1.9912094, 0.9912366] }` → Ack
Output levels **unchanged** after `peq set`. ✅ **Set is safe.**

```
minidsp -v output 1 peq 2 bypass off
```
Verbose output showed: `WriteBiquadBypass { addr: PEQ_4_8, value: false }` → Ack
Output levels: `-94.1, **0.0**, -94.1, -94.1` → **HANG on output 1.** ❌

**Key finding:** The hang is triggered by `WriteBiquadBypass { value: false }` (activating the filter), NOT by `WriteBiquad` (writing coefficients). Any real IIR filter causes this — not just HPF.

The miniDSP is always running (~-94 dBFS idle noise from analog inputs). Activating a real IIR biquad slot on a live DSP triggers the freeze. "No audio playing" does not mean "DSP is idle."

**Status:** Device needs physical reset. Next: test with master mute before `bypass off`.

---

### Session 3 — 2026-04-07

**Setup:** Device recovered (output 1 hung from session 2, outputs 2/3 clean).

**Test A: Peaking 80Hz with master mute before bypass off — output 2**

Sequence: `mute on` → `peq set` (real peaking) → `bypass off` → `mute off` → check status

Result: output 2 froze at 0.0 dBFS despite master mute. ❌

Finding: `SetMute { value: true }` only gates the analog output stage. The DSP filter pipeline continues running internally. Muting does NOT prevent the hang.

**Test B: Write to already-active slot (no bypass change) — output 3**

Sequence: `peq 2 clear` (writes identity + activates slot, `WriteBiquadBypass false`) → confirm output 3 at -94 dBFS ✅ → `peq 2 set` with real peaking coefficients → check status

Result: output 3 froze at 0.0 dBFS after `WriteBiquad` to an already-active slot. ❌

Finding: The firmware applies new coefficients IMMEDIATELY when `WriteBiquad` is called on an active slot. This means the hang isn't just about `bypass off` — any mechanism that makes real IIR coefficients active in the running DSP causes immediate overflow.

**Status:** All outputs hung (0: defective, 1-3: hung). Device needs physical reset.

**Root cause hypothesis — sign convention mismatch:**

The miniDSP 2x4 HD firmware almost certainly uses a POSITIVE-sign recurrence:
```
y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] + a1_stored*y[n-1] + a2_stored*y[n-2]
```

This requires `a1_stored = -a1_scipy` (negated). With scipy's negative a1, the effective poles are at `|z|≈2.4` (explosive), causing immediate saturation → 0.0 dBFS.

Evidence:
- Identity filter (a1=0): unaffected by sign → always works ✓
- Any real IIR (a1≈-2): immediately hangs when active → consistent with unstable poles ✓
- Negated a1 (+1.9912, -0.9912) would give poles at |z|≈0.99 → stable ✓
- Why mute doesn't help: DSP still runs internally, still overflows

**Next test (after reset): send real peaking with NEGATED a1/a2:**
```bash
# Scipy: a1=-1.9912, a2=+0.9912  →  Negated: a1=+1.9912, a2=-0.9912
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 set -- 0.9987203134966584 -1.9912093556606645 0.9925163375433332 1.9912093556606645 -0.9912366510399917 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp -v output 1 peq 2 bypass off 2>&1"
ssh pi@192.168.1.117 "sudo docker exec avr-calibration minidsp status 2>&1"
```
Expected: output 1 stays at ~-94 dBFS (stable) → confirms sign convention fix.

---

*Update this file after each test session with observed results.*

# Filter Design — Reference

A working reference for designing PEQ, FIR, and modal-cancellation filters from
measurement data. Companion to `docs/fr-interpretation.md` — that document
classifies *what* each measurement feature is; this one prescribes *how* to
design a filter to address it.

The audience is the LLM driving calibration. It needs (a) the math, (b) the
parameter-selection rules, (c) the iteration discipline, and (d) the canonical
references so it can defend a choice.

This document is **prioritized**. Read tiers in order on first encounter; revisit
specific sections when a design decision surprises you. Add to it whenever a
real run exposes a pattern not captured here.

## Priority ladder

- **Tier 1 — Foundational filter theory.** Required reading before designing
  any filter.
  - 1.1 Biquad filters and the Bristow-Johnson cookbook
  - 1.2 Q, bandwidth, and damping
  - 1.3 Min-phase / linear-phase / mixed-phase FIR
  - 1.4 Gain selection and stability margins
- **Tier 2 — Parameter selection from measurements.**
  - 2.1 PEQ from a measured peak
  - 2.2 PEQ for boundary gain / wide low-Q lifts
  - 2.3 PEQ for cancellation nulls (and when *not* to)
  - 2.4 Shelf design for target curve fitting
  - 2.5 HPF / LPF for crossover and protection
- **Tier 3 — Modal correction.**
  - 3.1 Why PEQ alone can't shorten T60
  - 3.2 Modal-FIR anti-pulse design (Gabor envelope)
  - 3.3 Cancel strength selection
  - 3.4 Multi-mode interactions and dense-mode handling
- **Tier 4 — Multi-source allocation.**
  - 4.1 Per-sub level, delay, polarity
  - 4.2 Welti SFM for variance reduction
  - 4.3 Per-sub cancellation strength: empirical results disagree with
        coupling-share intuition
  - 4.4 Treatment-type asymmetry breaks coherence
- **Tier 5 — Iteration discipline.**
  - 5.1 One variable per iteration
  - 5.2 Simulate-before-apply
  - 5.3 Regression handling
  - 5.4 When to stop
- **Tier 6 — System-level integration.**
  - 6.1 Slot budgeting (PEQ vs FIR vs sub-bus vs mains)
  - 6.2 Tool-reach order
  - 6.3 Convergence thresholds
  - 6.4 Listening test as the final gate
  - 6.5 Avoiding the flat-room failure mode

---

# Tier 1 — Foundational filter theory

## 1.1 Biquad filters — Bristow-Johnson cookbook

A *biquad* is a second-order IIR filter with the transfer function:

```
        b0 + b1·z⁻¹ + b2·z⁻²
H(z) = ─────────────────────
        a0 + a1·z⁻¹ + a2·z⁻²
```

Bristow-Johnson (RBJ 1995, "Cookbook formulae for audio EQ biquad filter
coefficients") gives closed-form coefficients for every parametric EQ shape
in production audio software. The same coefficient formulas appear in Web
Audio API, MiniDSP, CamillaDSP, REW, and most DAW plug-ins — they are the
de facto industry standard.

### The five shapes you need

| Shape | Use | Math identity |
|---|---|---|
| **peaking** (a.k.a. parametric / bell) | Boost or cut a band centered on f0 | One bell-shaped magnitude bump; phase wraps ±90° around f0 |
| **low_shelf** | Boost or cut everything below f0 with gentle slope | Magnitude transitions through Gain/2 at f0 |
| **high_shelf** | Boost or cut everything above f0 | Mirror of low_shelf |
| **HPF** (high-pass) | Cut everything below f0 | -12 dB/oct (Q=0.707 = Butterworth) or steeper at higher Q |
| **LPF** (low-pass) | Cut everything above f0 | Mirror of HPF |

(Notch and all-pass exist but rarely belong in a room cal toolkit; notch is a
peaking with infinite Q and is used sparingly because real modal peaks are
not infinitely narrow.)

### Why biquads stack

Cascading N biquads gives an order-2N filter. PEQ chains routinely cascade
6-16 biquads to assemble a target response. Order matters slightly for
finite-precision math but is invisible in float32 for our domain.

### Exact parameters at f0

For a peaking biquad with `Q` and `gain_db`, RBJ gives:

```
A = 10^(gain_db/40)            # amplitude in dB→linear, half-gain (peaking)
ω0 = 2π·f0/Fs
α = sin(ω0)/(2Q)
b0 = 1 + α·A
b1 = -2cos(ω0)
b2 = 1 − α·A
a0 = 1 + α/A
a1 = -2cos(ω0)
a2 = 1 − α/A
```

Implementations differ only in normalization choice; the audible result is
identical when `b0/a0 …` are reduced by `a0`.

### Reference: Bristow-Johnson, R. "Cookbook formulae for audio EQ biquad filter
coefficients." Web technical document, c. 1995.
URL: <https://webaudio.github.io/Audio-EQ-Cookbook/audio-eq-cookbook.html>

---

## 1.2 Q, bandwidth, and damping

Q (quality factor) is the most-misunderstood parameter in audio EQ. It
matters because mismatching filter Q to the *measured* feature's Q produces
under- or over-correction.

### Three Q definitions

| Definition | Formula | Used by |
|---|---|---|
| **Bandwidth Q** | Q = f0 / Δf where Δf is the -3 dB bandwidth | RBJ peaking biquad, REW, MiniDSP |
| **Damping ratio** | ζ = 1/(2Q) | Mechanical / acoustic theory |
| **Octave bandwidth (BW)** | Q = √(2^BW) / (2^BW - 1) | Music DSP, IIRFilterMicro |

Convert between octave BW and Q: `BW = ln((2Q² + 1)/(2Q²) + √((2Q² + 1)²/(2Q²)² − 1))/ln(2)`

Quick reference: Q=0.707 ≈ 2 octaves, Q=1.4 ≈ 1 octave, Q=2.9 ≈ ½ octave,
Q=5.8 ≈ ¼ octave, Q=11.5 ≈ ⅛ octave.

### Q-from-mode-data

Most measurement tools (`analyze_decay`, REW's "Modes" tab) report `suggested_q`
from the modal decay envelope. The relationship to filter Q is:

- For **peaking PEQ on a modal peak**: filter Q ≈ measured Q × (0.7 to 1.0).
  Slightly *narrower* (higher numerical Q) than the measured mode → cut the
  peak without dragging neighboring bands.
- For **boundary-gain or low-Q lift** (measured Q < 1.5): use filter Q ≈ 0.7
  (broad shelf-like cut) — mirroring the wide-Q lift.
- For **notch nulls**: don't. See §2.3.

### Damping ratio physics

For a single-degree-of-freedom oscillator (a single room mode), the modal
amplitude decay envelope is exp(-ζω0·t), where ζ = 1/(2Q). T60 (decay by 60 dB)
relates to Q by:

```
T60 = (3·ln(10) / π) · (Q / f0) ≈ 2.2 · Q / f0
```

A 70 Hz mode with T60=1000 ms implies Q ≈ 31.8 — a very high-Q mode. Real
in-room modes range from Q≈3 (mild damping) to Q≈30 (sparse, untreated rooms).

### Reference: Pierce, A. *Acoustics: An Introduction to Its Physical Principles*
(2nd ed., Acoustical Society of America 2019), Ch. 6 (modal analysis of
enclosures); Beranek, L. *Acoustics* (1986), Ch. 10. Smith, J.O.,
*Introduction to Digital Filters* (W3K Publishing, 2007), Ch. 3.

---

## 1.3 Min-phase / linear-phase / mixed-phase FIR

FIR (finite impulse response) filters offer arbitrary frequency response
shapes plus *control over phase response*, which biquads can't provide.
For room calibration the relevant choice is along the phase axis.

### Linear-phase FIR

- Constant group delay across all frequencies → no dispersion (transients
  preserve their shape).
- Cost: pre-ringing. The IR is symmetric about its midpoint; energy appears
  *before* the main impulse. For an N-tap linear-phase FIR at sample rate Fs,
  pre-ring is N/(2·Fs) seconds.
- 4096 taps @ 48 kHz → ~42.7 ms pre-ring.
- 24576 taps @ 48 kHz → ~256 ms pre-ring.
- Pre-ringing on transients is audible as a "doubling" or smear — typically
  unwanted in bass cal.

### Minimum-phase FIR

- Phase determined entirely by magnitude (Hilbert-pair). No pre-ring.
- Causal: all energy at or after the main impulse.
- Group delay is NOT constant — varies with frequency.
- Inverse exists and is also min-phase → fully invertible by another min-phase
  filter. This is what makes min-phase EQ tractable.
- Computed via cepstral method: `H_min = exp(IFFT(Re(Hilbert(log|H|))))`.

### Mixed-phase FIR

- Has both pre- and post-ring, but with deliberate placement to address
  excess-phase content (cancellation, all-pass behavior).
- Modal-cancellation FIRs are mixed-phase: anti-pulse before main impulse
  cancels the modal arrival at the mic, then main impulse delivers the rest.
- Pre-ring is **intentional** but bounded — typically ≤ Gabor half-length per
  mode (see §3.2).

### When to use which

| Goal | Choice |
|---|---|
| Speaker correction, magnitude only, no phase concerns | Min-phase FIR |
| Reference monitor — preserve transient timing | Linear-phase FIR (accept pre-ring trade) |
| Modal cancellation in a small room | **Mixed-phase modal FIR** (this codebase) |
| Crossover with flat phase | Linear-phase FIR (accept latency) |

### Why mixed-phase for modal correction

A room mode has long T60. The modal "ringing" at the mic is the room re-radiating
energy from its stored modal amplitude. To reduce it, you need to *destructively
interfere with the modal arrival before it builds up*. That's a pre-pulse arrival
at the mic — which means a non-causal (negative-time) impulse component, hence
mixed-phase. Min-phase EQ cannot do this; it can only reduce the source drive
into the mode (inputs less energy → mode is weaker but still rings).

### References

- Smith, J.O., *Introduction to Digital Filters* (W3K Publishing, 2007),
  Ch. 11 (FIR filters), Ch. 16 (minimum phase).
- Oppenheim, A.V. & Schafer, R., *Discrete-Time Signal Processing*
  (3rd ed., Pearson 2009), Ch. 5 (transform analysis), Ch. 7 (FIR design).
- Damera-Venkata, N., Evans, B., Toledo, V. (2000), "Modal-domain audio
  equalization using minimum phase filters." JAES 49(7-8).
- Mourjopoulos, J. (1985), "On the variation and invertibility of room impulse
  response functions." J. Sound Vib. 102(2). Foundational discussion of why
  *exact* room inversion is impossible (excess-phase content varies with
  position) but minimum-phase magnitude correction is well-posed.

---

## 1.4 Gain selection and stability margins

### Cuts are always safe; boosts cost headroom

A **cut** reduces signal in a band. The output never exceeds input → no
clipping risk from the EQ itself.

A **boost** increases signal in a band. Each +6 dB boost requires +1 bit of
headroom or a -6 dB pre-attenuation upstream. Stacking +12 dB of boost across
non-overlapping bands consumes the same headroom; overlapping boosts can
produce more aggregate gain than any single band.

In our pipeline:

- CamillaDSP master gain at -20 dB provides 20 dB of intentional headroom for
  boost stacking.
- Per-band cap: typically +6 dB.
- Cumulative cap in any 1/3 octave: typically +9 dB.
- These caps are codified in the `safety_profile` for each transducer and
  enforced by SafetyValidator in the codebase. They mirror published driver
  protection guidelines (e.g., SVS application notes for ported subs).

### Why boost ceiling matters more than cut floor

Beyond clipping, boosts have these failure modes:

- **Driver thermal limits** — boosting at the modal frequency where the driver
  is already excited can push voice coil temperature up faster than expected.
- **Excursion limits (Xmax)** — most relevant below port tuning. Boosting
  20 Hz on a sub tuned to 22 Hz causes excursion to climb without acoustic
  output gain (port doesn't load below tune). This stresses the driver
  mechanically with no audible benefit.
- **Compression** — when driver compression onset is near reference SPL, an
  EQ boost in the compressed band loses 50% or more of its electrical gain
  acoustically while heating the coil.

### Headroom math

For a target curve that asks +X dB above the natural response:

```
required_headroom_dB = max_per_band_boost + max_cumulative_boost_in_third_oct
```

In a typical config: 6 + 9 = 15 dB headroom. Plus 5 dB safety margin for
peak transients = 20 dB → matches the persistent CamillaDSP master gain.

### When to ignore the doc's "always cut, never boost" rule

- **Below port tuning** — don't boost; physics prevents the driver from
  delivering acoustic output, and you'll cook the coil.
- **At a real cancellation null** — don't boost; the null is excess-phase
  and EQ doesn't fill it (see §2.3).
- **Above driver Xmax limit at target SPL** — don't boost; check compression.
- **Boundary gain in deep bass** — DO cut. Boundary gain is min-phase and
  cleanly cuttable.

### References

- Linkwitz, S., "On the impact of equalization on perceived sound quality."
  AES Convention 113, 2002 (gain-cut philosophy and listener preference).
- Audio safety guidelines for ported subs: SVS PB12-NSD application notes;
  Funk Audio white papers on Xmax.

---

# Tier 2 — Parameter selection from measurements

## 2.1 PEQ from a measured peak

The canonical "fix this peak" recipe.

### Inputs

- f0 = peak center frequency (Hz)
- peak_db = excess of peak above the band's expected level (dB)
- measured_q = mode Q from `analyze_decay` or REW's modes table
- target_db = expected level at f0 from the calibration target curve

### Outputs

- filter type: peaking
- f0: ≈ measured peak center; for narrow modes pick the *exact* mode freq
- gain_db: see below
- Q: see below

### Gain selection

```
naive_gain = -(peak_db − target_db)              # exact cancellation
clipped    = max(-max_cut_per_band, naive_gain)  # respect floors
applied    = max(clipped, -peak_db + 2)          # leave 2 dB residual
                                                 # — overshooting is audible
```

Why leave 2 dB residual: a perfect cancellation cut sets the band to target
exactly at f0. But the room's modal phase is position-dependent — moving the
mic ±30 cm shifts the mode amplitude by 1-3 dB. A perfect cut at MLP becomes
a *negative* cut at the next listener position, audible as "thin." Leaving
residual averages out across the listening volume. (Toole, *Sound Reproduction*,
3rd ed., Ch. 17 — listener tolerance for departures from target.)

### Q selection

```
peq_q = clamp(measured_q × 0.85, 0.7, 12)
```

Use 0.85× rather than 1.0× because:

- Measured mode Q includes modal "spread" from coupling to neighboring modes
  — the underlying modal damping is slightly higher Q than what `analyze_decay`
  reports.
- A slightly narrower filter (numerically *higher* Q) than the measured mode
  keeps the cut from spilling into the bands either side, which preserves
  energy where it's wanted.

Clamp at Q=12 because filters with Q > 12 become exquisitely sensitive to
the exact f0 — measurement noise of 1 Hz at the mode center detunes the
filter by 1/12 of its bandwidth. Trim to 12 unless you have a known-pure
narrow resonance to attack.

### When to use a *single* PEQ vs split into multiple

For a single mode peak: 1 PEQ.

For multiple closely-spaced peaks (within ~1/2 octave): the dense-mode trap.
Filter skirts overlap, attempting to address each independently produces an
effective notch between them. **Combine** — use a single broader, lower-Q
PEQ centered between the modes.

For two peaks ~1 octave apart: 2 PEQs, separately. They don't interact
significantly.

### References

- Cooper, J. & Bauck, J. (1989), "Prospects for transaural recording." JAES.
  General principles of PEQ for room correction.
- Olive, S. & Welti, T. (2009), "The Influence of Room and Listener Position
  Variations on the Perceived Performance of Sound Reproduction Systems."
  AES Convention 127. PEQ behavior across listening positions.

---

## 2.2 PEQ for boundary gain / wide low-Q lifts

Below ~80 Hz, room boundaries (floor + walls + corner loading) produce
*broad* low-frequency lifts that are NOT modal but pure pressure-zone
gain. Signature: smooth +3-9 dB rise spanning 1-2 octaves. Doc
fr-interpretation §1.1.

### Recipe

- filter type: low_shelf (preferred) OR peaking with low Q
- f0: choose the corner where lift begins (typically 60-80 Hz)
- gain_db: -1 × measured_lift, clipped at safety cap
- Q: 0.4-0.7 (broad)

A low_shelf at 60 Hz with -6 dB gain, Q=0.7, accomplishes a clean broadband
attenuation of bass region without affecting the rest of the curve.

### Why this is *different* from modal

Boundary-gain features are min-phase and have **short T60** at the lifted
frequencies (the *room* isn't ringing; the boundary is just acoustically
loading the source). PEQ cuts here work straightforwardly — no FIR or bass
trap needed.

The diagnostic distinguishes:
- Long T60 at the lift = modal (use §2.1 + Tier 3)
- Short T60 at the lift = boundary (use this section)

---

## 2.3 PEQ for cancellation nulls — and when *not* to

A null in the FR can be either:

(a) A **modal pressure null** — the mic is at a node of the standing wave.
    Phase: continuous through the null. Position-dependent (small mic moves
    change the null amplitude dramatically).

(b) A **cancellation null** — two coherent arrivals 180° out of phase.
    Phase: rotates by ~π through the null (excess-phase). Includes:
    - SBIR (source's own output reflecting back to the source plane)
    - Sub-mains crossover misalignment
    - Inter-sub destructive interference

### NEVER boost a cancellation null with min-phase EQ

This is the most common cal mistake. The math: boosting `1/H(ω)` at a null
requires inverting the all-pass component of H. Min-phase EQ inverts the
magnitude but adds compounded excess phase, which **lengthens** the time-
domain effect and worsens transient quality without filling the magnitude
hole. The mic still measures a hole because the cancellation in the air
hasn't been undone — only the signal driving the cancellation has been
made larger.

(Mourjopoulos 1985 is the canonical reference: room IR is non-minimum-phase
and only the minimum-phase part is cleanly correctable with EQ.)

### When boosting *is* OK

For a modal node (a, above): boosting source drive does help — the mode is
less excited at this position because the standing wave puts a node here,
but the surrounding listener positions DO see the mode with normal amplitude.
A small boost (≤ +3 dB) in a narrow band can balance the listener volume.

For a cancellation null (b): the right intervention is *physical* — move
the source, treat the boundary, adjust delay/polarity between sources.

Use `analyze_phase` to distinguish. If the band is classified `geometry`
(near-π excess GD), do not EQ. If `fixable` or `partial`, EQ is appropriate.

### References

- Mourjopoulos, J. (1985), "On the variation and invertibility of room
  impulse response functions." J. Sound Vib. 102(2): 217-228. The
  foundational paper on why room inversion is fundamentally limited.
- Neely, S. & Allen, J. (1979), "Invertibility of a room impulse response."
  J. Acoust. Soc. Am. 66(1).

---

## 2.4 Shelf design for target curve fitting

For applying a target curve (Harman, in-room slope, custom), use shelves —
not stacks of peaking filters.

### Low_shelf for bass tilt

Harman bass tilt is +4 dB plateau 20-50 Hz, transitioning to 0 dB by 200 Hz.
This is **shelf shape**, not modal. Use a single low_shelf:

```
{type: low_shelf, freq: 50, gain_db: 4, q: 0.707}
```

Q=0.707 (Butterworth) is the classic choice for a smooth shelf transition
without overshoot.

### High_shelf for treble tilt

In-room speaker measurements typically show +1 dB / octave downward from
1 kHz to 10 kHz reflecting Toole's reference target. To impose this on a
flat-anchored measurement, a high_shelf at 1-2 kHz with -2 to -4 dB does
the job.

### Anchoring matters more than the shelf parameters

Anchor the target at the band where measured ≈ target *before* applying
boost shelves. Otherwise the system's natural response and the target
diverge in a way that asks for unfixable boosts in regions the room
can't deliver.

The mechanism: PEQ cuts at peaks deliver close to the asked dB at the
listener (the room reinforces what the source emits). PEQ boosts into
nulls deliver less than asked because room cancellation at the listener
position is a phase-cancellation phenomenon — adding source amplitude
into a null pumps energy into the room's modal cycle without filling
the cancellation at MLP. Specific delivery efficiency is room-, Q-, and
position-dependent; treat as "boosts are unreliable" not as a fixed
percentage. Anchoring at the curve's lowest-target band (e.g. 80 Hz =
0 dB on Harman+4) forces deep-bass into BOOST territory — boosts that
may not land. Anchoring at the curve's highest-target band (e.g. 25 Hz
= +5 dB on Harman+4) puts most of the work into CUTS at lower-target
bands, which do land. Use master gain to compensate for the absolute-
level drop the cuts create.

**Empirical sub-rule for anchor selection:** for each candidate
`f_anchor`, compute per-band gap = `(measured[f] − measured[f_anchor]) −
(target[f] − target[f_anchor])`. Pick the `f_anchor` with the lowest
worst-case gap. In modal-rich rooms this is often a band *just below*
the dominant hump (e.g. 31 Hz wins over 25 Hz when the hump sits 50-80
Hz), not the curve's natural 0 dB point.

### References

- Olive, S., Welti, T., & McMullin, E. (2013), "Listener Preferences for
  Different Headphone Target Response Curves." AES Convention 134.
  Empirical preference for shelf-shaped bass curves over flat.
- Toole, F., *Sound Reproduction*, 3rd ed., Ch. 12 — target curves and
  preferred slopes.

---

## 2.5 HPF / LPF for crossover and protection

### Mandatory infrasonic HPF

Every sub chain MUST have an HPF below the sub's port tuning (or below the
desired LF cutoff for sealed designs). Why: sub drivers are unloaded below
port tuning, voice coil excursion grows with input amplitude with no
acoustic gain. Music content below tuning (rumble, infrasound, mic-bumps)
will hammer the driver into Xmax.

For PB12-NSD (port tune ~22 Hz): mandatory HPF at 18 Hz, 4th-order
Butterworth, hard-coded by SafetyValidator.

For sealed subs (no port): HPF at the design Fc, 4th-order or steeper.

### Crossover filter choice

| Filter | Slope | At-xover loss | In-phase sum |
|---|---|---|---|
| **Linkwitz-Riley 24** (LR24) | -24 dB/oct | -6 dB each | flat (0 dB) |
| LR48 | -48 dB/oct | -6 dB each | flat |
| Butterworth 24 | -24 dB/oct | -3 dB each | +3 dB |
| Butterworth 12 | -12 dB/oct | -3 dB each | +3 dB |

LR (cascaded Butterworths of half order, doubled) is preferred for
sub-mains crossover because the in-phase sum is flat. BW gives a +3 dB peak
at xover that may or may not be desirable.

### Recommended xover frequency

For full-range subs with limited mains:
- 80 Hz LR24 (THX/AVR default) — works for most setups
- 60 Hz LR24 — when mains are large and full-range
- 100-120 Hz LR24 — when mains are small bookshelfs without enough bass

The doc `feedback_sub_vs_mains_delay_compensation.md` covers per-channel
distance/delay alignment around the crossover.

### References

- Linkwitz, S. (1976), "Active Crossover Networks for Noncoincident Drivers."
  JAES 24(1).
- Linkwitz/Riley filter overview: Linkwitz Lab tech notes.

---

# Tier 3 — Modal correction

## 3.1 Why PEQ alone can't shorten T60

A peaking PEQ cut at a modal frequency reduces the *source signal* drive
into the mode. The mode is a coupled resonant system between the speaker
and the room boundaries; once excited, it stores acoustic energy and
re-radiates it with its own characteristic decay envelope. The source
drive sets the *steady-state level*, but the *decay envelope* is a
property of the room, not the signal.

In practice:

- A -10 dB PEQ cut at 50 Hz reduces sustained 50 Hz output (e.g., a tonic
  bass note) by about 10 dB in the room.
- The same cut barely affects the *transient response* (e.g., a kick drum
  hit at 50 Hz) — the kick excites the mode, the mode rings out at its
  natural T60 regardless of the cut. The peak amplitude during the ring
  is reduced, but the *duration* of the ring is unchanged.
- Listeners describe this as "the bass is less booming but still muddy" or
  "kick drums lack snap, sustain is OK."

PEQ source-cuts deliver less than asked at the listener for transient
material when the perceived mode strength is dominated by ringing tail,
not steady-state amplitude. Specific delivery ratios are room- and
T60-dependent — don't expect a fixed percentage; expect that the
deficit grows with T60 / measurement-window ratio.

### Diagnostic discipline: dilution vs. pipeline bug

When a PEQ or FIR cut measures meaningfully less than the asked dB at
the listener, the *first* hypothesis should be **modal-ringing dilution
at this room's frequencies**, not a DSP/filter pipeline bug. The check
sequence:

1. Confirm filter parameters landed in the active pipeline
   (CamillaDSP `GetConfigJson` or equivalent). One SSH command.
2. Run `analyze_decay` and read T60 at adjacent modes.
3. If T60 > 400 ms in the band, the dilution is room physics. Reach
   for modal-FIR anti-pulse or bass traps before chasing pipeline bugs.

Skipping this check and going down a "filters silently ineffective"
debugging path costs hours when the explanation is already documented
physics.

### What actually shortens T60

Three options, ranked by effectiveness for a given installation cost:

1. **Bass traps (room treatment)** — physical absorption at modal frequencies.
   Doubling Sabine absorption halves T60. Most effective lever; permanent
   fix; requires physical space.
2. **Multi-sub SFM (Welti)** — multiple subs with per-sub level/delay/EQ
   reduce *mean* T60 across the listening area by exciting modes from
   different positions. See §4.2.
3. **Modal-FIR anti-pulse** — DSP-only mixed-phase filter that destructively
   interferes with the modal arrival at the mic. Effective at a single
   listening position, breaks down off-axis. Best DSP-only option for fixed-
   listener systems.

PEQ is *not* on this list. It addresses peak amplitude in steady-state,
which is a different perceptual axis.

### References

- Bharitkar, S. & Kyriakakis, C., *Immersive Audio Signal Processing*
  (Springer 2006), Ch. 5-6 (room equalization theory).
- Toole, F., *Sound Reproduction*, 3rd ed., Ch. 9 (low-frequency listening).
- Cox, T. & D'Antonio, P., *Acoustic Absorbers and Diffusers* (3rd ed.,
  CRC Press 2017), Ch. 5-6 (porous absorbers, membrane absorbers).

---

## 3.2 Modal-FIR anti-pulse design (Gabor envelope)

The anti-pulse is a short windowed sine pulse placed *before* the main
impulse in the FIR. When played, it creates a band-limited acoustic
arrival at the mic that destructively interferes with the modal ringing.

### Mathematical structure

```
fir_ir = anti_pulse(t + Δt) + main_impulse(t)
```

where `Δt` is chosen as the half-period of the target mode (T/2 = 1/(2·f0))
so the anti-pulse arrives 180° out of phase with the upcoming modal arrival.

### Anti-pulse shape: Gabor function

A Gabor function is a Gaussian-windowed sinusoid:

```
g(t) = exp(-t²/(2σ²)) · cos(2π·f0·t + φ)
```

It minimizes the time-frequency uncertainty product (Heisenberg / Gabor
limit) — the most localized signal in *both* time and frequency domains.

For modal cancellation, this means:
- Localized in frequency: only the target mode is affected, not neighbors.
- Localized in time: the anti-pulse fades quickly so it doesn't ring on
  its own.

### Gabor cycles parameter

The number of cycles in the Gabor envelope (`gabor_n_cycles`) sets the
trade-off:

- **Fewer cycles (n=2)**: shorter time window; shorter pre-ring budget;
  wider spectral skirts that can spill into adjacent 1/3-octave bands.
- **More cycles (n=4)**: longer time window; longer pre-ring budget; tighter
  spectral confinement.

Default n=3 in the literature (Heyser TDS, Smith DSP guide). For modal
correction in small rooms with multiple closely-spaced modes, lower n
(n=2) shortens pre-ring per mode and tightens time-domain
localization at the cost of wider spectral skirts; the resulting
trade is room- and mode-density-dependent. Test in simulation,
measure post-apply.

### Bandpass Q (`bp_q`)

A bandpass filter applied to the Gabor envelope before placement controls
how narrow the anti-pulse's spectral content is. Higher `bp_q` → narrower
band → less adjacent-band leakage but lower cancellation strength because
the energy is concentrated.

For modes at least 1/2 octave apart: `bp_q=1.5-2` (default).

For dense modes within 1/2 octave: `bp_q=3-5` to keep adjacent-band
leakage below the safety cap. **Trade-off: narrow Q reduces cancellation
strength** because the anti-pulse misses the mode's actual bandwidth.

### Cancel strength (cs)

Scales the anti-pulse amplitude relative to the main impulse:

```
anti_pulse_amplitude = main_amplitude × cs
```

- cs=0: pure passthrough, no cancellation.
- cs=1: anti-pulse equals main impulse magnitude. Maximum cancellation
  capability but uses full headroom.
- cs in [0.5, 0.85]: typical operating range.

`design_modal_fir` auto-fits cs downward if adjacent-band leakage exceeds
the safety profile's `modal_cancel_max_boost_db` cap. The achieved cs may
be smaller than requested — see §3.3 for the discipline around this.

### Pre-ring budget

The anti-pulse must fit before the main impulse:

```
pre_ring_ms = T_period/2 + Gabor_half_length
            = 1/(2·f_mode) × 1000 + n_cycles/(2·f_mode) × 1000
            = (1 + n_cycles)/(2·f_mode) × 1000  ms
```

For 47 Hz with n=2: pre-ring ≈ 31.9 ms. For 70 Hz with n=2: ≈ 21.4 ms.

When placing multiple anti-pulses, the budget is the max of any single
mode's required pre-ring. Lower-frequency modes dominate.

### References

- Gabor, D. (1946), "Theory of Communication." J. IEE 93(III).
  Original Gabor function paper.
- Heyser, R. (1967), "Acoustical Measurements by Time Delay Spectrometry."
  JAES 15(3). Foundational time-frequency analysis.
- Müller, S. & Massarani, P. (2001), "Transfer-Function Measurement with
  Sweeps." JAES 49(6). ESS deconvolution methodology.
- Toda, T. & Saruwatari, H. (2014), "Active Modal Cancellation Using
  Mixed-Phase Filters for Listening-Position Equalization." Proc. of the
  IWAENC. Application of mixed-phase / anti-pulse FIR for modal
  correction, including practical tuning of cancel-strength parameters.
- Karjalainen, M. & Paatero, T. (2007), "Equalization of Loudspeaker and
  Room Responses Using Kautz Filters: Direct Least Squares Design."
  EURASIP J. on Advances in Signal Processing 2007.

---

## 3.3 Cancel strength selection

> **Specific cs starting values are room- and mode-dependent.**
> Prior tabulated values (from this codebase, pre-2026-05-06
> architecture fix) were derived from measurements that have been
> invalidated. Re-derive cs starting points from valid solos under
> the corrected target-driven measurement chain before treating any
> table as definitive.

General principles (physics-grounded):
- Higher cs → stronger cancellation in the target mode but greater
  spectral leakage to adjacent bands.
- Longer T60 modes can tolerate higher cs because their dominant
  perceptual signature is decay-time, not steady-state amplitude;
  partial cancellation still produces audible T60 reduction.
- Dense-mode clusters (modes within ~1/2 octave) require lower cs
  per mode plus higher `bp_q` to avoid adjacent-band cap violations.
- Single-mode design first, dense-cluster design second; iterate the
  cs of any one mode independently before stacking.

After applying, measure and check:
- If peak amplitude reduces by ≥60% of `predicted_t60_reduction_pct` → working
  as designed. Iterate up to higher cs only if listener feedback wants more.
- If peak amplitude reduces less than half what's predicted → adjacent-band
  leakage probably auto-scaled cs down. Check the design's `cancel_strength_achieved`
  vs requested. Or the mode is partly excess-phase (not a clean modal target).
- If peak amplitude *grows* → polarity error in the design or measurement.
  Verify with a single-mode design first.

### Modal share as cs allocator across multiple subs — open question

> **NEEDS RE-VALIDATION.** Prior empirical observations (this codebase,
> pre-2026-05-06) suggested symmetric cs across subs outperformed
> per-sub proportional weighting. Those observations were on
> measurements that have been invalidated. The conclusion may or may
> not hold under valid solos taken under the target-driven chain.

What's defensible from physics: anti-pulse cancellation requires both
the cancellation arrival and the modal arrival at the mic to combine
destructively. Per-sub phase relationships at MLP determine whether
symmetric or proportional cs is correct — this can only be measured,
not deduced. Default to symmetric across identical drivers in
symmetric placements; reach for proportional only when per-sub solos
under valid chain show large modal-share asymmetry AND simulation
predicts proportional improves the combined response.

### Adjacent-band auto-scaling

`design_modal_fir` runs an iterative reduction: if any adjacent-band peak
exceeds the safety profile's `modal_cancel_max_boost_db` cap, cs is scaled
down until adjacent bands stay under cap. The scaling is **automatic and
asymmetric across modes** — if mode A's cs scales from 0.85 to 0.65 and
mode B is unscaled at 0.85, the achieved relationship between modes is
different from requested.

Always inspect `cancel_strength_achieved` per mode after design. If multiple
modes were auto-scaled to similar values, the system was probably trying to
over-cancel at the cap; reduce the requested cs proportionally to land on
the achieved value cleanly.

### Adjacent-band leakage is physics — don't hand-tune around it

When `design_modal_fir` trips a `FIR boost of +XX dB at <adjacent band>
exceeds modal-cancellation cap` warning, the boost is a real magnitude
property of the anti-pulse (constructive sum where the half-wavelength
delay produces in-phase addition), not a config mistake.

- **Don't** iterate `cancel_strength` / `bp_q` by hand searching for a
  value that just slips under the cap. The relationship isn't smooth:
  low cs is too weak to cancel; high cs hits the cap.
- **Do** rely on the iterative-reduction path that auto-binary-searches
  amplitude until adjacent bands fit the cap, and read back
  `cancel_strength_achieved`.
- **Don't** raise the `modal_cancel_max_boost_db` cap to brute-force a
  design through. The cap exists for thermal/excursion at the *driver*;
  the driver eats the boost regardless of what the room does with it.
- If achieved cs is too low for usable T60 reduction, the mode is in a
  dense neighbor cluster and the remaining DSP-side levers are higher
  `bp_q` (narrower envelope) or `compensation_notch`. Beyond that:
  physical bass traps.

---

## 3.4 Multi-mode interactions and dense-mode handling

### Closely-spaced modes (within 1/2 octave)

When two modes are within 1/2 octave (e.g., 117 + 140 Hz, ratio 1.197 ≈ 0.26
octaves), each mode's anti-pulse spectral skirt overlaps the neighbor's
band. Per the auto-fit behavior described in §3.3, the design tool either:

- Reduces cs on one or both modes to keep adjacent-band peak under cap.
- Auto-raises `bp_q` to narrow the spectral footprint, at cost of
  cancellation strength.
- Both, in tandem.

### Practical recipes

**Two modes 1 octave apart** (e.g., 47 + 70 Hz, ratio 1.49 ≈ 0.57 octaves):
- Default `bp_q=1.5-2`, full cs each. They don't interact significantly.

**Two modes 1/2 octave apart** (e.g., 70 + 117, 117 + 140):
- Either: `bp_q=2` on both, slightly lower cs (~0.7); or
- Drop one mode and treat with PEQ instead of modal-FIR (if it's lower-Q
  / shorter T60, it's not really modal — see §1.1).

**Three or more modes within an octave**:
- This is dense modal regime. Treat the strongest one or two with modal-FIR
  and the rest with PEQ. Or accept that modal-FIR will be partially effective
  and complement with bass traps.

**Modes within 1/4 octave** (very rare in real rooms):
- Don't try multi-mode anti-pulse. Treat as a single broader feature with
  one wider PEQ + bass trap.

---

# Tier 4 — Multi-source allocation

## 4.1 Per-sub level, delay, polarity

Sub array calibration begins *before* any EQ:

1. **Solo measurement per sub** at MLP. Get IR peak time and SPL.
2. **Polarity check**: IR peak signs should match. If one sub IR peak is
   negative-going and others are positive, that sub is wired/configured
   inverted. Flip *one* sub via `set_polarity` (not all — see
   `feedback_relative_sub_polarity.md`).
3. **Delay alignment**: subtract earliest IR peak from each, apply that
   delay to the earlier-arriving sub via `set_delay`. Re-measure: combined
   IR peak should land between the individual peaks; combined SPL should
   exceed any individual sub's SPL by 3-6 dB (in-phase sum).
4. **Level matching**: equalize per-sub SPL within ±1 dB at MLP.

Only after these are aligned does *any* EQ make sense.

### References

- Welti, T. (2002), "How Many Subwoofers Are Enough?" AES Convention 112.
- Welti, T. & Devantier, A. (2006), "Low-Frequency Optimization Using
  Multiple Subwoofers." JAES 54(5): 347-364. The canonical multi-sub paper.

---

## 4.2 Welti SFM for variance reduction

Welti & Devantier 2006 demonstrated empirically that with proper placement
and per-sub DSP, four symmetric subs reduce seat-to-seat variance by up to
6 dB compared to a single sub or random placement.

### Key results (from the paper)

| Sub config | Median variance dB (35-80 Hz, across listening area) |
|---|---|
| 1 sub random | ~4.5 dB |
| 1 sub corner | ~4.0 dB |
| 2 subs symmetric (front/rear midwall, OR L/R midwall) | ~3.0 dB |
| 4 subs symmetric (corners or midwalls) | ~2.0 dB |
| 4 subs random with EQ | ~3.5 dB |

Symmetry beats random + EQ. EQ doesn't fix variance; placement does.

### Implication for asymmetric sub layouts

Two subs at *asymmetric* positions (one corner, one nearfield, e.g.) cannot
achieve Welti-style variance reduction. Per-MLP optimization is the only
DSP lever, but it doesn't generalize to other seats. For multi-listener
applications:

- Either accept MLP-only optimization with off-axis variance (cinema with one
  sweet seat).
- Or invest in symmetric multi-sub placement (4 subs, midwall or corner
  symmetric).

---

## 4.3 Per-sub cancellation strength: open question

> **NEEDS RE-VALIDATION** — Earlier observations suggested symmetric cs
> across subs outperformed proportional-weighting (cs ∝ modal share).
> Those observations were based on measurements from a chain that has
> since been invalidated (2026-05-06 wrong-route bug). The conclusion
> may or may not hold under valid measurements. Treat as open until
> re-derived from solos taken under the corrected target-driven chain.

What's defensible from physics: cancellation is destructive interference
at MLP, and both subs contribute to the cancellation amplitude. Reducing
cs on the lower-contribution sub still reduces its cancellation share —
whether the *combined* cancellation suffers depends on the per-sub phase
relationship at MLP, which can only be measured, not deduced.

Default: until re-validated, design symmetric cs across identical-driver
subs in symmetric placements; reach for proportional only when per-sub
solos under valid chain show large per-sub modal-share asymmetry.

---

## 4.4 Treatment-type asymmetry breaks coherence

Physics: anti_pulse FIR is mixed-phase (contains excess-phase content);
linear_notch and min_phase PEQ are minimum-phase. When two sub outputs
combine at MLP, their phase-vs-frequency relationship determines whether
they sum constructively, partially, or destructively in each band.
Mixed-phase + min-phase don't combine cleanly across the band — phase
wraps appear in the combined response that aren't present in either
solo.

**Rule from phase-combination math: all subs in a multi-sub array
should use the same treatment family per mode.** Cancel-strength
asymmetry within the same treatment family is fine; treatment-type
asymmetry across subs is not. Specific-magnitude coherence-collapse
numbers from prior empirical observations are not currently re-validated
under the corrected measurement chain — the principle stands on phase
math; expect a meaningful coherence drop, not a specific dB number.

---

# Tier 5 — Iteration discipline

## 5.1 One variable per iteration

The single most important discipline. When multiple parameters change at
once, the result is *uninterpretable*: you don't know which change caused
the observed delta, so you can't refine.

Concretely: between two consecutive measurements, change exactly ONE of:
- A single PEQ filter's f0 / Q / gain
- A single modal-FIR mode's cs / bp_q / treatment type
- Per-sub delay / polarity / level
- A single shelf parameter
- A single channel's distance or amp-assign

Even when intuition says "just change two things and save a sweep" —
don't. The cost of uninterpretable results is far higher than the saving
of one sweep.

### Counterexample to the rule: full-system regenerate

When pushing a fully-redesigned FIR set (e.g., changing target curve from
Harman to in-room), it's acceptable to regenerate all FIRs together
*because the system is being explicitly placed in a new state*. The
discipline is at the *iteration boundary* — within an iteration sequence,
one variable per step.

---

## 5.2 Simulate-before-apply

For every PEQ change, run `simulate_eq` against the current measurement.
For modal-FIR changes, run `simulate_per_sub_fir` (when available) or check
the design's predicted-effect output.

If the simulated FR shows:
- Convergence toward target → apply.
- Divergence (the proposed filter would make it worse) → adjust before apply.
- Adjacent-band excursion above the safety cap → expect auto-scaling at
  apply time; pre-adjust to land at the desired final cs.

Why: the cost of a bad apply is one wasted sweep + the recovery iteration.
The cost of a sim is ~50 ms. Always sim first.

---

## 5.3 Regression handling

When an iteration regresses (worse than previous):

1. **Don't iterate further** — adding more changes on top of a regression
   compounds confusion.
2. **Roll back** to the previous known-good state by re-applying the
   previous iteration's filters.
3. **Diagnose the regression**: which band got worse? Phase change? Was
   the predicted FR wrong, or did the apply not land cleanly?
4. **Test ONE alternative hypothesis** for what to do differently. Apply it
   alone (single variable). Measure. Compare.

Repeated regressions (>2 in a row) usually signal a deeper methodology
problem, not parameter tuning. Stop and investigate.

---

## 5.4 When to stop

Three stopping criteria:

| Criterion | Indicator |
|---|---|
| **Convergence** | RMS deviation below threshold (typically 4 dB), no single residual > +6 dB above target |
| **Diminishing returns** | Last 2 iterations changed RMS by < 0.5 dB. Further iteration is chasing noise. |
| **Subjective sign-off** | Listener says "yes." This is the actual gate; numbers are diagnostic. |

The fourth, less-discussed: **time-of-night**. Tired calibrator = sloppy
calibrator. Stop, sleep, return.

---

# Tier 6 — System-level integration

## 6.1 Slot budgeting (PEQ vs FIR vs sub-bus vs mains)

Typical budget for a 5.1.4 system:

| Layer | Capacity | Use for |
|---|---|---|
| **AVR FIR per main** (1024 taps) | ~6 frequency cuts/boosts | Mains modal correction, target curve shaping |
| **Sub PEQ (per output)** | 8 slots / sub | HPF (mandatory), modal cuts, level shelving |
| **Sub FIR (per output)** | 24576 taps | Modal-FIR anti-pulse, deep modal correction |
| **CamillaDSP master** | shared | Master gain, level matching, headroom |
| **Sub bus input PEQ** | shelves only | Target curve, NOT modal correction |

The hierarchy reflects the principle "fix at the source": per-output FIRs
for per-sub modal work, per-output PEQ for per-sub level/HPF, sub bus PEQ
only for shared target shaping (Harman tilt etc.).

---

## 6.2 Tool-reach order

The reach order for per-sub correction depends on hardware capability, not
the recipe's phase label. Check `get_config.eq_capabilities.fir_capable`
before designing.

1. **design_fir** for per-sub modal correction *when the hardware is
   FIR-capable*. Highest leverage on T60: flattens magnitude AND shortens
   decay. PEQ cannot shorten decay, only reduce peak magnitude. Follow
   with `recommend_fir_phase` on the post-FIR solo to decide if
   mixed-phase is warranted.
2. **apply_eq** per output for PEQ cuts (mode peaks, level matching) —
   the right tool on FIR-incapable hardware (miniDSP 2x4 HD etc.), and
   the fallback when FIR isn't a fit (e.g., infrasonic HPF).
3. **apply_input_eq** is **target-curve-only** — Harman, cinema-bass,
   flat — the shared shape every output receives. **Never use it to
   notch individual modes**; modal cuts belong on the specific output
   that excites them.

Apply higher-leverage tools first; reach down only when the higher-leverage
tool is insufficient or the wrong fit (e.g., shelf shaping doesn't need FIR).

---

## 6.3 Convergence thresholds

| Domain | Threshold | Source |
|---|---|---|
| Sub-only Harman+4 RMS, 25-80 Hz | ≤ 3 dB | Recipe v2 |
| Mains-only RMS to target, 80-2000 Hz | ≤ 4 dB | Recipe v2 |
| Combined system RMS, 25-200 Hz | ≤ 4 dB | Olive 2013 |
| Sub-vs-mains acoustic alignment | |Δ| ≤ 2 ms at xover | `feedback_empirically_verify_sub_mains_alignment.md` |
| Per-1/3-oct max residual | < +6 dB above target | Recipe v2 |
| Coherence at any band where filters are designed | ≥ 0.8 | Tier 2.1 of fr-interpretation.md |

---

## 6.4 Listening test as the final gate

No measurement-derived metric replaces subjective evaluation. Toole 2008
established that listener preference correlates with objective metrics
(spinorama, target deviation) but is not perfectly predicted by them.
Olive's preference rating (PR) achieves R² ≈ 0.85 — meaning 15% of variance
in listener preference is unexplained by measurement metrics.

Reference content for the gate:
- **Tron Legacy "End of Line"** — bass impact + modal stress at 50/70 Hz
- **Edge of Tomorrow beach** — broadband transient, low-end weight
- **La La Land "Another Day of Sun"** — vocal clarity, midrange honesty

Listener says:
- **Sounds great** → record_lesson, save_calibration_run(converged=True), ship
- **Specific complaint** → matched action (boomy → check 70 Hz; thin →
  check 30 Hz; muffled vocals → check center 117 Hz cut depth)
- **Catastrophic** → restore_dsp_snapshot rollback

---

## 6.5 Avoiding the flat-room failure mode

A technically-flat anechoic-style FR at MLP does **not** match listener
preference for in-room playback. Olive (2004) and Toole (2018, ch.
9-10) both document via blind listener studies that preferred in-room
target curves exhibit a downward tilt from low to high frequency, with
a deep-bass plateau (the "Harman" target shape and its bass-extended
variants). A measurement-flat result diverges from this preference;
listeners describe such systems as lacking warmth, weight, or impact.

### Operating principle

- **Match target *shape*, not every dB.** The objective is RMS
  deviation from a tilted target (Harman / Harman+4 / cinema curve),
  not flatness. A measurement-flat system that violates the target's
  bass tilt fails the perceptual goal regardless of its RMS-vs-flat
  score.
- **Selective modal correction.** Cut narrow high-Q peaks that exceed
  the target by significant margin; leave broader gentle humps that
  fall within the target's tilt envelope alone. Specific Q/dB
  thresholds are room- and listener-dependent — derive from the
  current room's measurements, not from a universal rule.
- **Re-trim per-channel levels POST-FIR.** FIR application shifts
  per-channel acoustic gain. Auto-trim or pink-noise level-match
  should run *after* FIRs commit so trims compensate for the FIR's
  net level effect.
- **Subjective gate is the final stop criterion.** Numerical RMS
  convergence does not guarantee preferred sound. Reference-content
  listening with explicit success/failure criteria (kick punch,
  vocal clarity, sub depth, dialog body) is the binding test (§6.4).

### Reference

- Olive, S. "A Multiple Regression Model for Predicting Loudspeaker
  Preference Using Objective Measurements." 117th AES Convention
  (2004) — listener-preference target derivation.
- Toole, F. *Sound Reproduction* (3rd ed., 2018), ch. 9-10 — in-room
  target curves and listener preference.

---

# Quick-reference card

## "I see a peak. What filter?"

```
if T60 > 500 ms AND Q > 3:
    # Real modal — use modal FIR + PEQ cut residual
    modal_fir(anti_pulse, cs=0.6-0.85)
    peq_cut(gain=-(peak_excess - 2), q=measured_q*0.85)
elif T60 < 200 ms AND Q < 1.5:
    # Boundary gain or wide lift
    peq_cut(gain=-peak_excess, q=0.7)
elif Q > 6 AND narrow:
    # Tight resonance (cabinet, port issue, driver Fb out-of-band)
    investigate physical cause; PEQ as last resort
elif coherence < 0.8:
    # Re-measure first, don't design against noise
    re_measure with longer/quieter capture
```

## "I see a null. What filter?"

```
if analyze_phase classifies "fixable" or "partial":
    # Min-phase null — boost may help, with caution
    peq_boost(gain=+min(missing_dB, +6), q=measured_q*0.85)
    # Stop at +6 dB; if mode persists, position is the lever, not EQ
elif analyze_phase classifies "geometry":
    # Excess-phase cancellation — DO NOT EQ
    if SBIR (f matches c/(4*d_wall)):
        physical: move source from rear wall, treat boundary
    elif sub-mains crossover:
        adjust delay/polarity, NOT EQ
    elif inter-sub cancellation:
        per-sub delay/polarity
    elif mic in null:
        accept; not a real listener problem
```

## "How aggressive should I be?"

| Iteration | Allowable change |
|---|---|
| Initial cal | Up to safety caps; aggressive |
| Refinement | One variable, ±3 dB max per iteration |
| Polishing (RMS < 5 dB) | One variable, ±1 dB max |
| Listening-tuning | Always one variable; gain in 1 dB steps |

## "When do I stop?"

- RMS to target < threshold (typically 4 dB) → stop, listening test.
- Last 2 iterations changed RMS < 0.5 dB → diminishing returns; stop.
- Coherence at any designed-against frequency < 0.8 → stop, fix measurement.
- Listener satisfied → ship.
- 11 PM → sleep, resume tomorrow.

---

# References (canonical reading)

## Filter design

- Bristow-Johnson, R. "Cookbook formulae for audio EQ biquad filter
  coefficients." c. 1995. <https://webaudio.github.io/Audio-EQ-Cookbook/audio-eq-cookbook.html>
- Smith, J.O. *Introduction to Digital Filters with Audio Applications.*
  W3K Publishing, 2007. (Free online: <https://ccrma.stanford.edu/~jos/filters/>)
- Oppenheim, A.V. & Schafer, R. *Discrete-Time Signal Processing.* 3rd ed.,
  Pearson, 2009.
- Linkwitz, S. (1976). "Active Crossover Networks for Noncoincident Drivers."
  JAES 24(1).
- Linkwitz, S. (2002). "On the impact of equalization on perceived sound
  quality." AES Convention 113.

## Room equalization theory

- Mourjopoulos, J. (1985). "On the variation and invertibility of room
  impulse response functions." J. Sound Vib. 102(2): 217-228.
- Damera-Venkata, N., Evans, B., Toledo, V. (2000). "Modal-domain audio
  equalization using minimum phase filters." JAES 49(7-8).
- Karjalainen, M. & Paatero, T. (2007). "Equalization of Loudspeaker and
  Room Responses Using Kautz Filters." EURASIP J. Adv. Signal Proc. 2007.
- Bharitkar, S. & Kyriakakis, C. *Immersive Audio Signal Processing.*
  Springer, 2006.

## Multi-sub / sound field management

- Welti, T. (2002). "How Many Subwoofers Are Enough?" AES Convention 112.
- Welti, T. & Devantier, A. (2006). "Low-Frequency Optimization Using
  Multiple Subwoofers." JAES 54(5): 347-364.
- Antsalo, P., Karjalainen, M., et al. (2007). "Estimation of modal
  decay parameters from noisy response measurements." JAES 49(11).

## Modal cancellation / mixed-phase

- Toda, T. & Saruwatari, H. (2014). "Active Modal Cancellation Using
  Mixed-Phase Filters for Listening-Position Equalization." Proc. IWAENC.
- Heyser, R. (1967). "Acoustical Measurements by Time Delay Spectrometry."
  JAES 15(3).
- Gabor, D. (1946). "Theory of Communication." J. IEE 93(III).
- Müller, S. & Massarani, P. (2001). "Transfer-Function Measurement with
  Sweeps." JAES 49(6). Log-sweep ESS deconvolution.

## Listener preference and target curves

- Toole, F. *Sound Reproduction: The Acoustics and Psychoacoustics of
  Loudspeakers and Rooms.* 3rd ed., Routledge/Focal Press, 2018.
- Olive, S. (2004). "A Multiple Regression Model for Predicting Loudspeaker
  Preference Using Objective Measurements." AES Convention 116.
- Olive, S., Welti, T., & McMullin, E. (2013). "Listener Preferences for
  Different Headphone Target Response Curves." AES Convention 134.
- Olive, S. & Welti, T. (2009). "The Influence of Room and Listener Position
  Variations on the Perceived Performance of Sound Reproduction Systems."
  AES Convention 127.

## Acoustics fundamentals

- Pierce, A. *Acoustics: An Introduction to Its Physical Principles.*
  2nd ed., Acoustical Society of America, 2019.
- Beranek, L. *Acoustics.* Acoustical Society of America (re-issued 1986).
- Cox, T. & D'Antonio, P. *Acoustic Absorbers and Diffusers.* 3rd ed.,
  CRC Press, 2017.
- Schroeder, M. (1996). "The 'Schroeder frequency' revisited." JASA.

## Crossover and bass management

- Linkwitz Lab Technical Notes. <https://www.linkwitzlab.com/>
- Stuart, J.R. (1991). "Predicting the audibility of group delay distortion."
  AES Convention 91.
- Bews, R. & Hawksford, M. (1986). "Application of geometrical theory of
  diffraction (GTD) to diffraction at the edges of loudspeaker baffles."
  JAES 34(10). Boundary effects on speaker response.

# See also

- `docs/fr-interpretation.md` — measurement classification (companion doc)
- `docs/measurement-chain.md` — target-driven measurement chain architecture
- `MEMORY.md` — operational rules, hardware facts, architectural principles

# Acoustic Measurement Interpretation — Reference

A working acoustician's reference for interpreting measurement data in this project.
The audience is the LLM driving calibration: it has to know not just *what* the FR shows
but *why* a feature looks that way and *which interventions are physically capable* of
changing it.

This document is **prioritized**. Read the tiers in order on first encounter; revisit
specific sections when a measurement surprises you. Add to it whenever a real run
exposes a pattern not captured here.

## Priority ladder

- **Tier 1 — Foundational.** Required reading before designing any filter or
  declaring something "unfixable." If you skip these, you will misclassify features
  and waste DSP slots on physics you can't change.
  - 1.1 Magnitude FR — peaks and nulls
  - 1.2 Impulse response & time domain
  - 1.3 Delay measurement (the tricky one)
  - 1.4 Phase and group delay
  - 1.5 Decay, T60, and room regimes (Schroeder)
- **Tier 2 — Essential interpretation.** Always check before claiming "the data
  says X."
  - 2.1 Coherence and signal quality
  - 2.2 Crossover region behavior
  - 2.3 Spatial averaging (MMM vs MLP)
  - 2.4 Polarity is frequency-dependent
- **Tier 3 — Methodology.** What the measurement chain itself does to the data.
  - 3.1 Smoothing and windowing (FDW, gating)
  - 3.2 Minimum-phase vs excess-phase decomposition
  - 3.3 Sweep type, noise floor, dynamic range
  - 3.4 SPL calibration and weighting
  - 3.5 Near-field vs far-field, boundary loading
- **Tier 4 — Advanced / situational.**
  - 4.1 Distortion, compression, headroom
  - 4.2 Multi-sub sound-field management (Welti)
  - 4.3 Directivity and what the mic actually captures
  - 4.4 Subjective correlates (Toole / Olive)
  - 4.5 Target curves and psychoacoustics
  - 4.6 Step response, ETC, early reflections, ITDG

---

# Tier 1 — Foundational

## 1.1 Magnitude FR — peaks and nulls

A frequency-domain peak or null is just a number. Its *cause* (modal, SBIR,
sub-mains crossover, boundary gain, port resonance, mic in a null, measurement
artifact, …) determines whether EQ helps, hurts, or is irrelevant. **You cannot
infer cause from the FR alone.**

### Required minimum data before classifying a feature

- **Phase character** — minimum-phase vs excess-phase (`analyze_phase`).
  Min-phase is EQ-correctable in principle; excess-phase (cancellation between
  two arrivals) is not.
- **Decay** — T60 / EDT at the feature frequency (`analyze_decay`). Long
  ringing in the modal region (>300 ms) implies stored room energy; short decay
  implies summation/boundary, no T60 problem.
- **Coherence** — <0.8 means the feature may not be real. Don't design against
  measurement noise.
- **Solo vs combined** — measure each source alone, then together; distinguishes
  inter-source cancellation (sub-sub, sub-mains) from room behavior.
- **Position variance** — move the mic ~30 cm and re-measure. Modal pressure
  nulls move sharply; SBIR notches follow the *source*, not the listener;
  boundary gain is roughly stable.
- **Predicted modes** — `f₁ axial = c / (2L)`, harmonics at integer multiples;
  oblique modes `f = (c/2)·√((nₓ/Lₓ)² + (n_y/L_y)² + (n_z/L_z)²)`. If the peak
  isn't near a predicted mode, it isn't modal.
- **Source-to-boundary geometry** — SBIR notch frequency `f = c / (4d)` where
  `d` is source-to-boundary distance.

### Nulls — not all are modal cancellation

| Cause | Signature | EQ verdict |
|---|---|---|
| **Modal cancellation** (pressure null at mic) | Excess-phase wrap at null freq; strongly position-dependent (moves with small mic shifts); coherence often holds; predicted by room geometry | Unfixable with EQ at *this* position. Move mic/sub or use multi-sub averaging. |
| **SBIR** — speaker-boundary interference: source's own output bounces off rear/floor/side wall and arrives inverted at the source plane | Notch at `f = c / (4d)` where `d` = source-to-boundary distance (e.g., ~85 Hz null when sub is ~1 m off the rear wall). Follows the source, not the room. Broadens with greater distance from boundary. | EQ won't fix (excess-phase). Fix is moving the source or treating the boundary (absorber / bass trap on that wall). |
| **Sub-mains crossover cancellation** | Null near crossover (60–100 Hz typical), depth varies dramatically with relative delay/polarity between sub and main; flips toward summation when polarity inverted | Fix with delay/polarity/level, NOT EQ boost. |
| **Inter-sub cancellation** | Null disappears or shifts when one sub is muted; `compare_sub_phase` shows opposing phase at null freq | Per-sub delay or polarity. EQ won't help. |
| **Mic in a pressure null** | Strongly position-dependent; nearby mic positions look completely different | Move mic. Not a real listener problem unless someone sits there. |
| **Below port-tune rolloff** | Smooth slope, not a notch; phase rolls off normally; predicted by sub design (PB12-NSD ~22 Hz tune) | Not a null. Don't boost — excursion-limited; you're hammering the driver into Xmax with no acoustic output. |
| **Port out-of-phase region** | Narrow dip just below port tune (~15–20 Hz on ported subs) where driver and port outputs cancel | Hard physical limit. HPF above it. |
| **Measurement artifact** | Low coherence (<0.8), inconsistent across repeat sweeps, sensitive to ambient noise | Re-measure with longer/quieter capture. Don't design against it. |

### Peaks — not all are room modes

| Cause | Signature | EQ verdict |
|---|---|---|
| **Axial / tangential / oblique room mode** | Frequency predicted by room dimensions. Long T60 (>300 ms) at the peak. Minimum-phase at peak. | Cutting at source reduces drive into the mode but T60 ringing persists — the room rings on its own. Modal-FIR anti-pulse or physical absorption (bass traps) is what shortens the tail. |
| **Boundary gain / pressure zone** | Broad lift below ~80 Hz from corner/wall loading. Roughly +3 dB per nearby boundary (floor + wall + wall = +9 dB at corner). Minimum-phase, no excess T60. | Safe to cut with PEQ. No ringing problem to chase. |
| **Port resonance** | Narrow lift just above port tuning (e.g., 25–30 Hz region for PB12-NSD). Part of the sub's intentional design — that's where the port radiates. | Don't aggressively cut. You're killing intended output and forcing the driver to make up the SPL with cone excursion. |
| **Sub-mains constructive summation** | Peak at the crossover region; flips to a null when polarity is inverted on either source | Fix with delay/level. EQ cut at the source is a workaround, not a fix. |
| **Driver Fb / cone breakup** (above sub passband) | Out-of-band peak; only matters if your LPF lets it through | Tighten LPF. Don't EQ inside the band you've already filtered out. |

---

## 1.2 Impulse response & time domain

The IR is the most informationally dense single measurement we have. Magnitude
FR is its squared magnitude in the frequency domain. Phase, decay, ETC,
spectrogram, group delay — all of it derives from the IR.

### Reading the IR

- **Direct sound:** first significant peak. Polarity is the sign of that peak;
  amplitude scales as 1/r at distance r (free field) or higher (boundary
  loading). Time of arrival = `r / c`, where c ≈ 343 m/s.
- **Floor bounce:** for source height `h_s` and mic height `h_m`, both at
  horizontal distance `d`, the bounce path is `√(d² + (h_s + h_m)²)`. Extra
  path → time delay → comb null at `f = c / (2·Δpath)` (first null) or
  `f = c / Δpath` (first peak).
  - Example: h_s = 1.0 m, h_m = 1.2 m, d = 3 m → direct = 3.18 m, bounce = 3.79 m,
    Δ = 0.61 m → first null near `c/(2·0.61) ≈ 280 Hz`. This is why "floor bounce"
    notches commonly appear 200–400 Hz.
- **Ceiling bounce:** identical math with ceiling height.
- **Side-wall reflection:** depends on geometry; for symmetric MLP, both sides
  arrive nearly together → constructive comb in the early window.
- **Rear wall (SBIR for sub):** the source's own output reflected back to the
  source plane → notch at `f = c/(4d_rear)`. This is *not* the same as a wall
  reflection at the listener; it's a source-side cancellation. See §1.1.
- **Modal ringing:** appears as a slow exponential decay tail extending well
  beyond the early reflections (10s of ms to 100s of ms). The frequency content
  of the tail is the modal energy; spectrogram/waterfall reveals it.

### Pre-ringing vs post-ringing

- **Causal min-phase systems:** all energy after `t = 0`. Any pre-ring in the
  IR is an artifact of processing.
- **Linear-phase FIR:** symmetric IR — pre-ring is the cost of phase linearity.
  Length `N/2` samples on each side of the main lobe at sample rate `Fs`.
  An 8192-tap linear-phase FIR @ 48 kHz has ~85 ms pre-ring.
- **Min-phase FIR:** all energy after the main lobe. No pre-ring. Trade: phase
  is whatever the magnitude requires (Hilbert-related).
- **Excess-phase systems** (room + speaker with cancellation): pre-ring can
  appear because the system has zeros outside the unit circle (right-half-plane
  zeros, in continuous-time). Trying to EQ those with min-phase filters will
  introduce more excess-phase, not cancel it.

### Polarity from the IR

A negative-going first peak at the direct-sound time = inverted polarity. For
sub alignment, inverted polarity flips the sub-mains summation from peak to
null at xover; check this before reaching for delay tools.

Real-system polarity is **frequency-dependent** in the strict sense (see §2.4) —
"polarity inverted" is shorthand for "phase ≈ 180° across the operating band."

---

## 1.3 Delay measurement

There are at least four different "delays" we extract from data, and they
disagree — sometimes by tens of milliseconds. Pick the right one for the
question.

| Method | What it measures | When to use | Failure mode |
|---|---|---|---|
| **First-arrival onset** | Time of first IR sample crossing a threshold above noise floor | Time-of-flight when SNR is high and pre-ring is absent | Threshold-sensitive; noise floor pulls onset earlier; linear-phase FIR pre-ring fakes an early arrival |
| **IR peak time** | Argmax of \|IR\| | Bulk delay between similar systems (sub-only vs sub-only at different position) | Pre-ring shifts peak; broadband systems with smeared IR have ambiguous peak |
| **Cross-correlation peak** | argmax of x*y for two signals | Bulk delay between two arbitrary signals, even with different FR shapes | Picks the delay that maximizes overall correlation — gives a *band-averaged* answer that may not match the integration band you care about |
| **Group delay at frequency** | `τ_g(ω) = -dφ/dω` evaluated at ω₀ | Delay *at a specific frequency* — the right answer for sub-mains integration in the crossover band | Noisy where coherence is low; meaningless across phase wraps without unwrapping |

### The sub-vs-mains delay specifically

Sub-vs-mains delay is **not a single number**. The sub chain has frequency-
dependent group delay (steep crossover filters add 5–15 ms; FIR processing adds
N/(2·Fs) for linear-phase; bass-management LPF/HPF ringing adds a few ms). The
mains chain has its own GD profile.

What you actually want to align is the group delay *at the crossover band*
(typically 60–100 Hz). Aligning at 1 kHz is wrong: bass content doesn't live
there. Aligning by IR peak is wrong: the peaks may correspond to different
parts of the response (HF for mains, LF for sub).

**Practical rule:** measure sub-only IR, mains-only IR, and combined IR. The
combined IR's behavior in the crossover band tells you whether they're
integrating. If the magnitude in the xover band is **−6 dB lower** than the
in-phase summation, they're 90° apart. If **−∞**, they're 180°. If **+3 dB
above** each individual, they're aligned in-phase.

### FIR latency in the delay budget

Linear-phase FIR introduces `N/2` samples of pure delay. At Fs = 48 kHz:

- 4096 taps → 42.7 ms
- 8192 taps → 85.3 ms
- 16384 taps → 170.7 ms

If only the sub chain is FIR-corrected, the mains have to be delayed by the
same amount or the sub will arrive late. AVR per-channel distance/delay is the
mechanism (see Audyssey envelope-bypass memory). Lip-sync delay is a different
mechanism (delays everything together to match video).

### Onset detection is content-dependent

- **Log sweep** (ESS): excitation density ∝ 1/f → equal energy per octave;
  deconvolution gives a clean impulse with high SNR at low freq.
- **MLS:** pseudo-random binary sequence; sensitive to time variance (HVAC,
  noise) and nonlinearity.
- **Music / pink noise:** stochastic; onset is statistical, not deterministic.

For the same physical system, log-sweep, MLS, and music-derived "first arrival"
can disagree by ms. Use one method consistently within a session.

### AVR-reported distance ≠ acoustic distance

- The AVR converts the user's distance (meters) to per-channel sample delays
  at its internal sample rate.
- Audyssey envelope effects, MultEQ filter latency, and amp-stage processing
  can add tens of ms that are not reflected in the user-visible distance.
- The X3800H clamps user-set distance at ~18 m (60 ft) in the UI; envelope-
  bypass via SET_SETDAT NotFin/Fin can extend the *applied* delay to ~55 ms
  equivalent (see Audyssey envelope-bypass memory).
- Bottom line: distance fields are a *control surface*, not a measurement.
  Verify with a real IR.

---

## 1.4 Phase and group delay

Phase is the angle of the complex transfer function `H(ω) = |H(ω)| · e^{jφ(ω)}`.
Group delay is `τ_g(ω) = -dφ/dω` — the delay each frequency component
experiences through the system.

### Phase wraps vs real excess

Phase is computed mod 2π. A naive plot wraps every ±π. An apparent "phase
discontinuity" near ±180° is usually a wrap, not physics. Always work with
**unwrapped phase** for delay/group-delay analysis. In modal regions with deep
nulls, unwrapping itself is ambiguous (phase can rotate by ±π through a null) —
verify by cross-checking with group delay.

### Linear, minimum, and all-pass phase

- **Linear phase:** `φ(ω) = -ωτ`, i.e., constant group delay. Implementable
  only by symmetric FIR. No dispersion: a transient stays a transient.
- **Minimum phase:** for a given magnitude response, the causal system with
  the smallest group delay; magnitude and phase are Hilbert-pair related.
  Inverse exists and is also min-phase → fully EQ-correctable.
- **All-pass:** `|H(ω)| = 1` for all ω, but `φ(ω)` ≠ 0. Pure phase shift
  without magnitude change. Used to flatten group delay or compensate other
  all-pass terms. Not invertible by min-phase EQ.
- **Excess phase:** `φ_excess = φ_total - φ_min`. Visible as bumps in excess
  group delay. Indicates non-min-phase content (cancellation, all-pass behavior).
  **EQ-uncorrectable from one position with one filter.**

### Group delay around the crossover

In a well-aligned sub-mains crossover, group delay should vary smoothly through
the xover band — typically a few-ms hump corresponding to the LR4/BW filter GD
maximum, but no jump or notch.

A **GD spike** at xover indicates relative delay misalignment. Correcting it
needs delay (group-delay shift), not EQ.

A **GD step** that flattens once polarity flips on one source: the polarity
was wrong; the apparent delay was a 180° phase difference being interpreted
as a half-cycle delay.

### Reading the phase plot during cal

- Smooth, gently sloping unwrapped phase = clean integration.
- Phase wraps at frequency `f` correspond to GD ≈ `1/f` in the local sense
  — useful sanity check.
- 180° phase rotation across a narrow band with a magnitude null = excess-
  phase null (cancellation). EQ cannot fix it; delay/polarity might.
- 180° phase rotation across a narrow band with a magnitude *peak* = pole/
  resonance. Min-phase. EQ-fixable.

---

## 1.5 Decay, T60, and room regimes

### Decay metrics

- **T60** — time for sound energy to decay by 60 dB after the source stops.
  Measured in practice from -5 dB to -65 dB on the integrated decay curve.
  In small rooms, the dynamic range to measure 60 dB is rarely available;
  T20 or T30 are extrapolated.
- **T20** — fitted slope from -5 to -25 dB, ×3 to estimate T60.
- **T30** — fitted slope from -5 to -35 dB, ×2 to estimate T60.
- **EDT (Early Decay Time)** — slope from 0 to -10 dB, ×6. Correlates with
  *perceived* reverberance better than T60 (which is dominated by the late
  diffuse tail).

### Sabine's equation

`T60 = 0.161 · V / A` (SI units; V in m³, A in m² of total absorption units)

Tells you the upper bound on decay shortening from absorption. Doubling
absorption halves T60. Real rooms deviate from Sabine when absorption is high
(Eyring correction) or when the field isn't diffuse (modal regime).

### Schroeder frequency — the regime boundary

`f_s ≈ 2000 · √(T60 / V)` (Hz; T60 in s, V in m³)

Below `f_s`: **modal regime.** Discrete eigenmodes dominate. Position matters
massively. Statistical assumptions break. Frequency response is jagged.

Above `f_s`: **statistical / diffuse regime.** Mode density is high enough that
the field is approximately diffuse. Position matters less. Frequency response
smooths out. Sabine's equation is well-behaved.

**Why this matters for bass calibration:** for a typical home theater
(V ≈ 60 m³, T60 ≈ 0.4 s), `f_s ≈ 163 Hz` — meaning the entire bass region
(20–200 Hz) is modal. EQ at one position does not generalize to another. Multi-
sub placement (Welti, §4.2) attacks this; single-position EQ does not.

### Modal density

Number of modes below frequency `f` (rectangular room):

`N(f) ≈ (4π/3) · V · (f/c)³ + (π/2) · S · (f/c)² + (L/8) · (f/c)`

where V = volume, S = surface area, L = total edge length. At low frequency,
modes are sparse and discrete; at the Schroeder frequency, mode density is
~3 modes per modal bandwidth (empirical threshold for "diffuse").

### Reading a waterfall / spectrogram

- **Vertical ridges** = modal ringing at specific frequencies. Height of ridge
  above floor = strength; length = decay time.
- **Smooth uniform decay across frequency** = absorption-dominated, statistical.
- **One frequency ringing 3× longer than its neighbors** = strong axial mode
  with low absorption at that frequency.
- **Floor noise rising at low frequency** = HVAC / traffic / sub self-noise;
  affects coherence (§2.1).

### Decay vs feature classification

- Long T60 (>300 ms) at a peak → **modal**. Reduce drive (PEQ cut) AND shorten
  tail (modal FIR / bass traps). PEQ alone won't change perceived ringing.
- Short T60 (<150 ms) at a peak → **boundary/summation**. PEQ cut is sufficient.
- Long T60 at a null → unusual; usually means modal nulls (the mode rings, but
  this position is at a node where the magnitude is low). Don't try to boost.

---

# Tier 2 — Essential interpretation

## 2.1 Coherence and signal quality

Coherence between input `x` and output `y`:

`γ²(ω) = |S_xy(ω)|² / (S_xx(ω) · S_yy(ω))`

`0 ≤ γ² ≤ 1`. 1 = perfect linear relationship. 0 = signals are uncorrelated.

### Thresholds

- `γ² ≥ 0.9` — high confidence; trust the FR/phase.
- `0.8 ≤ γ² < 0.9` — usable; treat narrow features with skepticism.
- `0.5 ≤ γ² < 0.8` — degraded; don't design narrow filters here.
- `γ² < 0.5` — noise/nonlinearity dominates; the FR at this freq is fiction.

### Causes of low coherence

- **Background noise floor** (HVAC, traffic, fridge) — usually below 80 Hz in
  homes; subs often sit barely above noise floor in the deep bass.
- **Nonlinearity** — driver compression, port chuffing, amp clipping. The
  output contains energy not predictable from the input.
- **Reflections beyond the gating window** — for windowed measurements, late
  arrivals look like uncorrelated noise.
- **Multiple uncorrelated paths** — if a userland audio bridge was tapping
  the signal asynchronously, the recorded "input" wasn't actually what
  played. Hard architectural rule on this rig: CamillaDSP+Focusrite is a
  single entity, no external audio bridges.
- **Time variance during measurement** — fan speed change, mic drift, person
  walking through.

### Practical implication

If a feature you want to fix has γ² < 0.8, the highest-leverage move is to
**measure better**, not to design a filter. Longer sweep, quieter room, mic
on a stable mount, repeat sweeps and average.

---

## 2.2 Crossover region behavior

### Filter-shape summation rules (acoustic, both sources flat in their bands)

| Filter | Slope each | Loss at xover (each) | In-phase sum at xover |
|---|---|---|---|
| Butterworth 2nd | -12 dB/oct | -3 dB | +3 dB peak |
| Butterworth 4th | -24 dB/oct | -3 dB | +3 dB peak |
| Linkwitz-Riley 2nd | -12 dB/oct | -6 dB | flat (0 dB) |
| Linkwitz-Riley 4th | -24 dB/oct | -6 dB | flat (0 dB) |
| Bessel | various | varies | small bump |

LR4 is the AVR/HT default because in-phase summation is flat. BW2 sums to a
+3 dB peak — sometimes desirable, sometimes a problem.

### Acoustic xover ≠ electrical xover

The electrical xover is where the filter is set. The acoustic xover is where
the *driver outputs cross* — shifted by driver natural rolloff and by phase
behavior. A sub with steep natural rolloff at 35 Hz combined with an 80 Hz LPF
has an effective xover lower than 80 Hz.

### Misalignment signatures

- **+3 to +6 dB peak at xover (LR4)** = sources in phase but with a small
  delay error; the "in-phase summation" assumption is violated.
- **Deep null at xover** = ~180° phase difference (polarity or large delay).
- **Asymmetric xover** (peak on sub side, null on main side) = both polarity
  and delay error.
- **Variable xover behavior across mic positions** = position-dependent
  arrival time difference; usually a sub placement or relative-delay issue.

### Phase tracking through xover

In an ideal integration, sub and main have the *same phase* in the xover
overlap region. Plot both phase responses on the same axes; they should
overlay through the xover band (typically ±1 octave around the filter
frequency).

---

## 2.3 Spatial averaging — MMM vs MLP

### MMM (moving mic method)

Mic moved continuously through a small volume during a single measurement.
Pros: averages out modal nulls in the listening volume; perceptually relevant
for someone moving on a couch. Cons: smears coherent features (xover
misalignment, SBIR) into apparent randomness; can hide a serious MLP problem.

### Multi-position spatial average

Discrete measurements at N positions (typically 5–9), then average in **power**
(not voltage) — `|H_avg|² = (1/N) · Σ|H_n|²`. Captures the listening volume's
average behavior.

### What averaging *does* fix

- Position-dependent modal nulls (each position has a different null, average
  smooths them).
- Single-position floor-bounce comb (averages partly out across head-height
  variation).

### What averaging does **not** fix

- **SBIR** — follows the source, not the listener. Every position sees the
  same notch.
- **Sub-mains misalignment** — universal at all positions in the listening area.
- **Driver/processing problems** — appear identically everywhere.
- **A really bad MLP** — averaging a great seat with three terrible seats can
  hide a problem at the seat people actually use.

### Best practice for cal target

Use the spatial average for the broad target curve (gross response shape).
Verify at MLP with single-point measurement before declaring done. If MLP and
average disagree significantly in a band, decide which seat matters more.

---

## 2.4 Polarity is frequency-dependent

In real systems, "polarity inverted" is shorthand for "phase ≈ ±180° across
the operating band." The actual phase response is continuous and varies with
frequency.

### Per-band polarity check

When `optimize_sub_alignment` recommends "all subs inverted," that almost
always means the *relative* polarity between subs is wrong — one sub has the
opposite polarity from the others, and inverting all of them is a wrong global
fix that re-creates the same relative problem.

Correct procedure:

1. Compare per-1/3-octave-band phase between subs (`compare_sub_phase`).
2. Identify the band where one sub disagrees with the others by ~180°.
3. Invert that one sub.
4. Re-verify per band — they should now agree across the full operating range
   (within ±45° is usually sufficient for clean summation).

### Polarity in the IR

A negative-going first peak at the direct-sound arrival time = inverted
polarity *at low to mid frequencies*. At very high frequencies relative to
the system bandwidth, IR peak sign is less reliable because the IR is more
oscillatory.

---

# Tier 3 — Methodology

## 3.1 Smoothing and windowing

### Frequency smoothing

Smoothing averages the FR with a kernel in log-frequency. Common choices:

- **1/3 octave** — close to perceptual; hides narrow features. Standard for
  RTAs and target curves.
- **1/6 octave** — useful balance; reveals modes without showing measurement
  noise.
- **1/12, 1/24 octave** — closer to raw at low freq (modal bandwidth ≈ 1/24
  oct in typical rooms); reveals mode structure.
- **None (raw)** — required for IR-derived analyses (phase, group delay).

**Smoothing changes what you can see.** A 6 dB peak at 47 Hz that's 1/12-oct
wide may look like a 2 dB lift in 1/3-oct smoothing. Don't design narrow PEQs
based on heavily smoothed data.

### Frequency-dependent windowing (FDW)

Apply a short time window at high frequency (capture only direct sound, reject
reflections) and a long window at low frequency (capture full modal energy).
Justified by psychoacoustic time-frequency resolution: the ear roughly
integrates over ~1/f cycles.

Default: ~5 cycles per octave below 200 Hz, narrowing to ~10 cycles above.
Enables comparing speaker direct sound to in-room behavior in one plot.

### Time gating

Rectangular (or windowed) time selection on the IR before FFT. The frequency
resolution of the gated FR is `Δf = 1 / T_gate`:

- 5 ms gate → 200 Hz resolution. Useful for direct-sound speaker measurements
  above ~500 Hz; cannot resolve modal behavior.
- 50 ms gate → 20 Hz resolution. Captures early room behavior.
- 500 ms gate → 2 Hz resolution. Captures full modal decay; needed for accurate
  bass FR.

You **cannot** measure modal bass with a short gate. The mode rings for
hundreds of ms; a 5 ms gate truncates the ringing, so the FFT shows a smooth
response that doesn't exist in the room.

### Window edge artifacts

Gating with a rectangular window introduces side lobes (sinc convolution in
frequency). Windowed gates (Tukey, Hann) reduce side lobes at the cost of
slightly worse main-lobe resolution. For modal-region measurements, prefer
long gates and accept the side-lobe behavior.

---

## 3.2 Minimum-phase vs excess-phase decomposition

Any causal stable system can be factored:

`H(ω) = H_min(ω) · H_excess(ω)`

where `H_min` has all zeros inside the unit circle (min-phase, EQ-invertible)
and `H_excess` is all-pass (`|H_excess(ω)| = 1`, phase-only).

### Computing H_min from |H|

For min-phase systems, phase is determined by magnitude via the Hilbert
transform of `log|H|`. `analyze_phase` computes this and compares with
measured phase to find the excess-phase component.

This is **only valid** if you trust that the system is min-phase. Real rooms
are mostly min-phase in the modal regime, but cancellations (SBIR, comb
filtering, sub-mains misalignment) introduce excess-phase content. The
diagnostic is exactly: "subtract Hilbert-derived min-phase from measured
phase; what's left is excess phase."

### Excess group delay

`τ_excess(ω) = τ_total(ω) - τ_min(ω)`

A bump in excess GD at a frequency = non-min-phase content there. Almost always
indicates a cancellation between two arrivals.

### EQ correctability

Min-phase content: fully invertible. PEQ that mirrors `1/H_min(ω)` recovers
flat magnitude and zero phase distortion in principle.

Excess-phase content: not invertible by min-phase EQ. Trying to flatten the
magnitude of an excess-phase null adds arbitrary phase distortion, often
worsening time-domain behavior. The right interventions are physical (move
the source) or mixed-phase FIR (which can in principle invert all-pass
sections, with care).

---

## 3.3 Sweep type, noise floor, dynamic range

### Excitation signals

- **Log sine sweep (ESS)** — frequency increases logarithmically. Excitation
  density ∝ 1/f → equal energy per octave. Best low-frequency SNR per unit
  measurement time. Robust to mild nonlinearity (harmonic distortion shows as
  acausal artifacts that can be windowed out post-deconvolution). **Default
  choice for room measurement.**
- **Linear sine sweep** — equal Hz/s; concentrates energy at high freq.
  Inferior bass SNR.
- **MLS (Maximum Length Sequence)** — pseudo-random binary; sensitive to time
  variance and nonlinearity. Largely superseded by ESS.
- **Pink noise** — equal energy per octave but stochastic; needs long
  averaging; useful for SPL calibration and subjective listening, less good
  for IR extraction.

### Dynamic range

The measurement's noise floor sets the smallest feature you can resolve. Sweep
length doubles → 3 dB more SNR (energy averaging). A 60 s log sweep typically
yields 90+ dB SNR in the bass region in a quiet room.

### Pre-emphasis / amplitude shaping

Some stimuli weight low-freq energy higher to compensate for room and driver
rolloff. Increases low-freq SNR at the cost of headroom at LF.

### Background noise considerations

- Sub bandwidth often sits within HVAC noise floor (40–80 Hz). Measure with
  HVAC off if possible; otherwise expect coherence drops.
- Traffic noise dominates 30–100 Hz outdoors-near-the-house; window-side
  positions are worst.
- Room natural ambient: typically 30–40 dB(A) for a quiet residential space.

---

## 3.4 SPL calibration and weighting

### Reference levels

- **Cinema reference (THX/SMPTE):** 85 dB SPL C-weighted at MLP from -20 dBFS
  pink noise per channel. Bass channel +10 dB hotter (mains 75 dB SPL each
  from -20 dBFS, so reference monitoring gain set so pink hits 85 dB SPL).
- **THX 75 dB SPL pink noise reference:** alternative widely used in home cal.
- **Sub channel level:** +10 dB from mains in cinema standard; in home cal,
  often individually trimmed by ear.

### Weighting curves

- **A-weighting** — approximates the 40-phon equal-loudness contour. Strongly
  attenuates bass. Standard for noise exposure and quiet-environment rating.
  *Not appropriate for cinema bass reference.*
- **C-weighting** — flat through bass and mids; rolls off above ~10 kHz.
  Standard for cinema/HT reference levels.
- **Z (flat / unweighted)** — what you usually want for measurement analysis.

Always state weighting when reporting SPL. "75 dB SPL" without weighting is
ambiguous; the bass interpretation can swing 20+ dB.

### Time integration

- **Slow (1 s)** — used for steady tones and reference calibration.
- **Fast (125 ms)** — used for transient program material.
- **Impulse (35 ms)** — peak transient measurement.
- **LEQ** — energy-equivalent continuous level over a measurement period;
  used for exposure and program loudness.

### Mic calibration

UMIK-1 / UMIK-2 ship with individual `.cal` files. The mic correction must
be applied during measurement (frequency-dependent gain) for the recorded FR
to be calibrated. Without it, expect ±2 dB errors below 30 Hz and above 10 kHz.

The 94 dB SPL @ 1 kHz pistonphone calibration (or USB mic factory cal) sets
the absolute SPL reference. Verify periodically.

---

## 3.5 Near-field vs far-field; boundary loading

### Near-field measurement

Mic placed within ~0.3 m of the driver. Captures the source output before
significant room interaction. Useful for:

- Driver health (T/S parameter changes, voice-coil rub, port chuff).
- Distortion measurement (THD, IMD) without room confounds.
- Sub anechoic-equivalent FR (Keele's near-field method): mic at port mouth
  + mic at cone, sum the contributions.

Near-field measurements **don't** represent listener experience.

### Far-field measurement

Mic at listening position (~3+ m). Captures the room-modified response —
what the listener actually hears. Used for the cal target.

### Boundary loading

A source close to a rigid boundary radiates into a half-space instead of full
space → +6 dB acoustic gain at low frequency. Two boundaries (floor + wall) →
+12 dB. Three (corner) → +18 dB. Real rooms approach these limits asymptotically
at very low frequency; at higher frequency the boundary dimension matters
relative to wavelength.

Practical:

- **Sub in the corner** → maximum boundary gain at deepest bass; drives all
  modes equally (every mode has a pressure maximum in a corner). Useful for
  efficiency, terrible for evenness.
- **Sub at midwall** → couples to half the modes; can null some.
- **"Sub crawl"** assumes the mic, placed at the listener, sees the same
  response as the sub would at that position (acoustic reciprocity). The
  reciprocity is exact in a linear time-invariant system.

### Pressure-zone concept

Near a boundary at frequencies where wavelength >> dimensions, pressure
doubles (boundary acts as a perfect mirror). Used in PZM mics. Same physics
as boundary gain.

---

# Tier 4 — Advanced / situational

## 4.1 Distortion, compression, headroom

### THD (total harmonic distortion)

Sum of energies at integer multiples of f₀ relative to f₀. Driver THD typically
rises rapidly below port tuning (mass-controlled region) and at high SPL
(excursion or thermal limits). Sub THD <5% in the ported region at moderate
SPL is typical for quality subs; >10% suggests over-driven.

### IMD (intermodulation distortion)

Two-tone test: tones at f₁ and f₂, look for sidebands at `m·f₁ ± n·f₂`. More
audible than THD because the sidebands aren't harmonically related. Usually
correlated with THD but not identical.

### Compression

The driver's SPL output rises less than 1:1 with input drive at high level.
Causes: voice-coil heating (resistance increases → less current → less force),
mechanical limits (suspension nonlinearity, Xmax), thermal modulation. A
compression sweep (input level vs output SPL at a fixed frequency) reveals
the headroom limit.

### Practical implications

- Cal at moderate level (75–85 dB SPL pink noise reference). Cal at very high
  level adds compression to the measured FR — you'll EQ to compensate for
  compression that doesn't exist at lower listening levels.
- Don't trust deep-bass FR within 6 dB of compression onset.
- A boost EQ at a frequency where the driver is already compressing buys
  nothing acoustically and stresses the driver thermally.

---

## 4.2 Multi-sub sound-field management (Welti)

Welti's seminal work (Harman, ~2003): with multiple subs and per-sub level/
delay/EQ, optimize for **seat-to-seat variance** in addition to MLP target.
Key results:

- Two subs symmetrically placed (front/rear midwall, or side-midwall pair)
  cancel one axial mode dimension, halving variance.
- Four subs in symmetric placement cancel two dimensions; achieves ~6 dB
  variance reduction across a wide listening area.
- Random sub placement with EQ can match a single sub at MLP but does not
  reduce variance.
- The win is *not* deeper bass; it's evenness across listeners.

### Implication for our setup

With a small number of subs (1–2) and asymmetric placement, we cannot achieve
Welti-style variance reduction. The strategy becomes:

- Optimize MLP with full DSP capability (per-sub delay, polarity, EQ, FIR).
- Accept that other seats will see different responses.
- Spatial averaging captures the rest of the listening area; if it diverges
  badly from MLP, the sub layout is the lever — not more EQ.

---

## 4.3 Directivity and what the mic captures

### Directivity Index (DI)

`DI = 10 · log₁₀(I_on-axis / I_spherical-avg)`

- 0 dB = omnidirectional (radiates equally in all directions).
- +3 dB = hemispherical (radiates into half-space; e.g., flush-mounted).
- Higher DI = more directional.

### Frequency dependence

- Subwoofers at modal frequencies: omnidirectional. Wavelength >> driver
  size; sub radiates to all corners equally.
- Mains in the bass: increasingly omni below ~200 Hz (driver size << λ).
- Mains in the mid-treble: directivity rises with frequency; off-axis listener
  hears reflections-dominated response.
- Mains at very high freq: typical 6–12 dB DI; tweeter directivity narrows.

### Mic captures vs listener perceives

A mic at MLP captures **a single point in space**. At low freq this is
representative. At high freq the listener's head is a finite size and moves;
the listener integrates over angle, the mic does not. Reflections that the
mic captures coherently the listener may localize separately or fuse based
on Haas-zone timing.

For bass cal this is mostly moot; for mains cal it justifies caring about
directivity-corrected analyses (spinorama, in-room weighted target).

---

## 4.4 Subjective correlates (Toole / Olive)

Floyd Toole and Sean Olive (Harman) established empirical mappings from
measurement to listener preference rating.

### Predictors of preferred speaker performance

- **On-axis FR smoothness** above 200 Hz — strongest single correlate.
- **Listening window** (±10° H, ±10° V) FR smoothness — almost as strong.
- **Early reflections smoothness** — secondary.
- **Bass extension** — preferred lower; -3 dB at ≤30 Hz scores best.
- **Sound power** smoothness (whole-sphere energy) — tertiary; matters for
  perceived spaciousness.

### Olive's preference rating (PR)

A regression formula from spinorama features predicting median listener
preference. Available implementations: spinorama.org, Sean Olive's papers.
Useful as a second-opinion when comparing speaker candidates.

### Implication for cal

- **Bass:** extension and SPL capability dominate preference; surface
  smoothness secondary. Don't aggressively flatten if it costs extension or
  output capability.
- **Mains:** on-axis smoothness matters more than absolute target shape.
  Match the speaker's natural off-axis trend rather than fighting it.
- **Slope:** in-room target should slope down ~1 dB/octave from 100 Hz to
  10 kHz, reflecting the speaker's typical sound-power rolloff.

---

## 4.5 Target curves and psychoacoustics

### Equal-loudness contours

Fletcher-Munson / ISO 226 contours show that human hearing's frequency
response is level-dependent: bass and treble require more SPL than midrange
to be perceived as equally loud, and the effect is stronger at low listening
levels. Cinema mixes assume reference monitoring (85 dB), so playback at
75 dB sounds bass-light unless compensated.

### Common bass targets

- **Harman in-room** — flat 200 Hz–20 kHz with a gentle 1 dB/oct downward
  tilt above 200 Hz; +4 to +6 dB lift below 200 Hz reaching peak around
  40 Hz; rolls off below 25 Hz to driver capability.
- **Bruel & Kjaer (1974)** — broadly similar bass lift; older.
- **X-curve (cinema)** — flat to 2 kHz then -3 dB/oct rolloff for screen
  channels; specific to large rooms with screen acoustic loss.
- **Custom tilts** — +6 dB bass for "fun" voicing; 0 dB ("flat") for analytical
  listening; depends on listener taste and content.

### Target curve choice ≠ EQ aggression

A target curve says "this is the desired in-room FR." It does not say "force
the system to follow this everywhere." Anchoring (see anchor-target memory) —
choose the band where measured ≈ target as the reference, cut peaks above,
accept boost-shy regions below — produces better-sounding results than
forcing the entire curve.

### Slope vs feature smoothness

Listeners rate speakers/rooms by **smoothness of departure from target**, not
by absolute target shape over a wide range. Within ±2 dB of any reasonable
target, choice of curve is taste. Above ±4 dB excursions, target shape barely
matters — the bumps dominate.

---

## 4.6 Step response, ETC, early reflections, ITDG

### Step response

Integrated impulse response. Reveals time-domain coherence between drivers in
a multi-way speaker (do tweeter and woofer arrive in-phase?) and overall
system polarity. Less informative for full-room behavior than IR/ETC.

### ETC (Energy-Time Curve)

`ETC(t) = 10 · log₁₀(|h(t)|²)`

The IR's squared envelope on a dB scale. Reveals reflections clearly: each
reflection is a discrete spike above the noise floor. Easier to read than
the raw IR for reflection identification.

### Reflection arrival times (typical home theater)

- **Floor bounce**: ~1–2 ms after direct (depends on geometry).
- **Ceiling bounce**: ~3–6 ms.
- **Side walls**: 5–15 ms.
- **Rear wall (from listener)**: 10–25 ms.
- **Front wall (behind speaker)**: usually obscured by speaker proximity.

### ITDG (Initial Time Delay Gap)

Time between direct sound and the first significant reflection. Perceptually
important: longer ITDG → larger-room impression. Cinema rooms target ITDG
>15 ms via early-reflection absorption. Small rooms naturally have short
ITDG and benefit from sidewall and ceiling treatment.

### Early vs late reflections

- **<50 ms** — early; perceptually fuses with direct sound, alters timbre and
  imaging.
- **50–80 ms** — transitional; can be perceived as discrete echo for transients.
- **>80 ms** — late; perceived as reverberance.

These thresholds are content-dependent (Haas zone for speech is shorter than
for music) and approximate.

### Bass: time-domain effects

In the modal regime, "reflections" lose meaning — the room rings as a coupled
modal system, not as a discrete sequence of bounces. Below Schroeder, work
in the modal/decay framework (§1.5), not the reflection framework. Above
Schroeder, ETC and reflection analysis is the right lens.

---

# Common pitfalls / anti-patterns

- *"It's at 45 Hz so it's a room mode"* — could be SBIR if the sub is ~2 m
  from a rear wall (`f = 343/(4·2) ≈ 43 Hz`).
- *"It's a null, EQ can't fix it"* — true only for excess-phase nulls. A null
  caused by two coherent sources cancelling is fixed with delay/polarity, not
  declared unfixable.
- *"Cuts are always safe"* — true for SPL/safety, but cutting a port resonance
  kills intended output, and cutting an SBIR notch source-side does nothing
  audible at the listener (the cancellation happens in the air, not in the
  signal).
- *Designing a filter against a feature with coherence < 0.8* — measurement
  noise, not a target.
- *Calling everything in 30–80 Hz "modal"* — boundary gain, port lift, and
  sub-mains crossover artifacts all live there too.
- *Aligning sub-mains by IR peak time* — IR peaks may correspond to different
  parts of the response. Use group delay at the crossover band.
- *Trusting AVR distance fields as absolute time references* — they're a
  control surface; verify with a real IR.
- *Reading FR with heavy 1/3-oct smoothing and designing narrow PEQ* —
  smoothing hides what you're trying to fix; design at 1/12 oct or finer.
- *Measuring with a 5 ms gate and reading bass FR off it* — modal energy
  arrives over hundreds of ms; you've gated it out.
- *Cal at high SPL and listening at moderate SPL* — driver compression
  contaminated the cal target.
- *Treating spatial average as ground truth* — it hides position-specific
  problems at MLP. Always cross-check single-point.
- *"All subs inverted" recommendation accepted globally* — that means
  *relative* polarity is wrong; flip one, not all.
- *Using A-weighted SPL for cinema reference* — wrong weighting; bass will
  read 20 dB lower than C-weighted standard.
- *Designing min-phase EQ to fill an excess-phase null* — adds phase
  distortion and pre-ringing; the null doesn't fill.
- *Ignoring FIR latency in the delay budget* — sub chain late by tens of ms,
  every measurement reads sub as misaligned, you chase a delay error that's
  actually FIR group delay.
- *Trusting first-arrival onset detection on noisy IR* — noise pulls onset
  earlier; cross-check with cross-correlation or group delay.

---

# Quick-reference card

## "Is this feature EQ-fixable?"

- Min-phase + good coherence → yes.
- Excess-phase (cancellation, SBIR, sub-mains misalignment) → no, fix the
  cause.
- Below sub capability or above sub passband → no, out of bounds.
- Coherence <0.8 → re-measure first.

## "What kind of intervention does this feature need?"

| Feature | Likely cause | Intervention |
|---|---|---|
| Long-T60 peak in 30–80 Hz | Modal | PEQ cut + modal FIR / bass trap |
| Short-T60 broad lift below 80 Hz | Boundary gain | PEQ cut |
| Narrow lift at 25–35 Hz | Port resonance | Leave alone |
| Notch at f matching `c/(4d_wall)` | SBIR | Move source / treat boundary |
| Notch near xover | Sub-mains delay/polarity | Adjust delay/polarity |
| Notch that disappears when one sub muted | Inter-sub cancel | Per-sub delay/polarity |
| Notch only at this mic position | Mic in null | Move mic |
| Smooth rolloff below ~22 Hz | Below port tune | HPF, don't boost |
| Anything with coherence <0.8 | Measurement | Re-measure |

## "Which delay number do I want?"

- Bulk delay between two systems → cross-correlation peak or IR peak.
- Sub-mains alignment → group delay at the crossover band (60–100 Hz).
- Pure time-of-flight → first-arrival onset (high-SNR IR only).
- Speaker A vs Speaker B in-band consistency → group delay across overlap.

## Required diagnostics before declaring something "unfixable"

1. `analyze_phase` — min vs excess?
2. `analyze_decay` — T60 at the freq?
3. Coherence in the band — trustworthy?
4. Solo vs combined — inter-source or single-source?
5. Mic +30 cm — position-dependent?

---

# See also

- `feedback_peq_cannot_suppress_modal_ringing.md` — PEQ source cuts deliver
  only 1–2 dB at the listener for true modal peaks
- `feedback_anchor_target_at_natural_band.md` — anchoring target curves in
  modal-rich rooms
- `feedback_filter_dilution_check_first.md` — check T60 at adjacent modes
  before blaming the DSP pipeline
- `feedback_solo_vs_combined_check.md` — per-band check for destructive
  interference
- `feedback_relative_sub_polarity.md` — "all subs inverted" = relative
  polarity wrong
- `feedback_deepbass_align_and_delay_sweep.md` — align in the boost band, not
  wideband
- `feedback_empirically_verify_sub_mains_alignment.md` — measure sub-only +
  mains-only + combined IR after FIR/distance changes
- `project_modal_fir_gabor_truncation_bug.md` — known design_modal_fir issue;
  context for current FIR state

# References (canonical reading)

- Toole, F. *Sound Reproduction: The Acoustics and Psychoacoustics of
  Loudspeakers and Rooms* (3rd ed.). The reference for in-room measurement
  interpretation and listener preference.
- Welti, T. & Devantier, A. "Low-Frequency Optimization Using Multiple
  Subwoofers." JAES, 2006. Multi-sub SFM theory.
- Olive, S. "A Multiple Regression Model for Predicting Loudspeaker
  Preference Using Objective Measurements." AES Convention 116, 2004.
- Schroeder, M. "The 'Schroeder frequency' revisited." JASA, 1996.
- Müller-Trapet, M. *Measurement of Surface Impedance and Sound Absorption.*
  Building physics methodology.
- Heyser, R. "Acoustical Measurements by Time Delay Spectrometry." JAES, 1967.
  ETC origin.
- Farina, A. "Simultaneous measurement of impulse response and distortion
  with a swept-sine technique." AES Convention 108, 2000. Log-sweep IR
  method.

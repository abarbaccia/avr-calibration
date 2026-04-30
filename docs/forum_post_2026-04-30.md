# LLM-driven home theater calibration: replacing Audyssey's automation with a Pi, CamillaDSP, and Claude

After about a month of building, my home theater calibration stack now does
something I haven't seen anyone else do: **a large language model (Claude)
drives the whole calibration loop**, picking targets, designing filters,
and writing them straight into the AVR's MultEQ filter banks via the same
TCP path the official Audyssey app uses.

Three pieces snapped together to make this possible. Posting them here in
case any of it is useful for others doing similar work.

## TL;DR

* Replaced the miniDSP 2x4 HD with a Pi 5 + Focusrite Scarlett 18i20
  running CamillaDSP. ~20× more FIR taps available per sub channel,
  cleaner signal path, and a Linux box I can actually code against.
* The FIR designer is driven by Claude (the LLM) — it reads measurements,
  classifies modes (long-ringy → anti-pulse cancellation; short-loud → linear
  notch; gentle → min-phase EQ), enforces SafetyValidator caps, and
  iterates toward a target. Tools are dumb and provide *data + simulation*;
  judgment lives in the LLM.
* Built a Python implementation of the Audyssey TCP/1256 protocol that
  writes per-channel polyphase-decimated FIRs directly to the Denon
  X3800H. No MultEQ Editor app, no .ady round-trip — Claude can issue
  filter changes as MCP tool calls.

GitHub: https://github.com/abarbaccia/avr-calibration

## 1. Sub chain: out goes the miniDSP, in comes CamillaDSP on a Pi

The original setup put a miniDSP 2x4 HD between the Denon's LFE pre-out
and the subs. It worked, but two limits hurt:

* **PEQ-only** (10 biquad slots per channel). PEQ can knock down a modal
  peak's *magnitude* but doesn't shorten the modal *ringing* (T60). A
  loud 47 Hz mode with T60=600 ms still rings 600 ms after a PEQ cut —
  you've reduced the input energy but the room keeps going.
* **No introspection over the network**. The 2x4 HD has a USB control
  surface. Useful for a human, useless for an LLM driving an iteration
  loop.

The replacement chain is:

```
Denon LFE pre-out → Scarlett 18i20 ADC (USB) → snd-aloop → CamillaDSP
                  → Scarlett DAC outputs 5/6 → SVS PB12-NSD subs
```

CamillaDSP is a fantastic open-source DSP engine that runs on the Pi.
What it unlocks for sub calibration:

* **Long FIRs.** 4096 taps per channel at 8 kHz processing rate gives a
  512 ms filter window with ~2 Hz frequency resolution per bin. That's
  sharp enough to surgically target narrow 23 / 47 Hz modes.
  Specifically: anti-pulse cancellation works because we can place a
  band-limited inverted impulse one half-wavelength before the main
  impulse and shorten T60 in the time domain — not just attenuate the
  peak.
* **Multi-rate sandwich.** CamillaDSP captures at the Scarlett's native
  48 kHz, resamples to 8 kHz internally for the long FIR, resamples back
  to 48 kHz for the DAC. The CPU saving (≈6×) makes the long-window
  filter feasible on a Pi 5; the AsyncSinc Balanced resamplers add
  ~10 ms of latency on each side, which we now compensate for upstream.
* **Live introspection.** The daemon exposes a websocket — Claude can
  query processing load, capture rate, mute state in real time.
* **Pi 5 has 25× more headroom than we use** — 1.4-2% load with the
  4096-tap FIR + resamplers running. Plenty of room to grow the filter
  size or run multiple subs.

The biggest gotcha along the way: the Scarlett's `plughw` ALSA device
silently inserts kernel-side resampling and adds a buffer. Switching to
raw `hw:` cuts ~5-10 ms of asymmetric capture-vs-playback latency, but
**only if your CamillaDSP processing rate matches a rate the device
supports natively**. We briefly tried `hw:` while still at 8 kHz
processing — Scarlett doesn't accept 8 kHz natively, and the playback
path silently dropped 7-23 dB across 25-100 Hz. Reverting to `plughw`
fixed it. (TODO: full migration to 48 kHz native + `hw:` + 24,576-tap
FIR for the same 512 ms filter window without the resampler.)

## 2. LLM as orchestrator: filter design as judgment, not algorithm

The thing I'm most pleased with: **Claude makes every meaningful
calibration decision**. Python provides the analytics + math + hardware
I/O; the loop logic and "what to do next" lives in an LLM-driven recipe.

The architecture rule we settled on:

> Tools provide data + simulation. The LLM provides judgment.
>
> Don't build deterministic solvers for decisions the LLM should make.
> If a tool contains for-loops that decide *what* to correct, *where* to
> place filters, or *which* frequencies to target — that decision belongs
> to the LLM.

Concretely, what this looks like:

* `analyze_decay` returns the room's modes (frequency, T60, peak magnitude,
  Q). It does **not** decide which to treat or how.
* `analyze_phase` decomposes the response into minimum-phase + excess-phase
  components, classifies bands as "fixable with EQ" vs "geometry-bound
  cancellation" (no amount of boost will fill a phase null at MLP).
* `compute_deviation` reports RMS error against a target curve, with
  geometry bands automatically excluded so the loop doesn't iterate
  past convergence chasing nulls.
* `simulate_eq` predicts the post-EQ FR before any hardware write —
  Claude iterates in simulation until satisfied, then writes once.
* `design_modal_fir` takes per-mode `intent` objects from Claude
  (`anti_pulse | linear_notch | min_phase | skip`) plus parameters like
  `cancel_strength`, `bp_q`, `max_pre_ring_ms` — and emits coefficients.
  Claude writes the recipe; the math is just convolution.

The "types of injection" Claude reasons about:

| Treatment | When | Why |
|---|---|---|
| `anti_pulse` (Gabor envelope) | T60 > 2× target, peak > +6 dB | Cancels the mode in time domain — both magnitude AND ringing tail. Costs ½-wavelength of pre-ring per mode (e.g. 7 ms at 70 Hz). |
| `linear_notch` | Short loud peak (T60 < 0.5× target, peak > +12 dB) | Surgical magnitude cut, ~3-5 ms pre-ring. |
| `min_phase` | T60 moderately above target | Conservative magnitude EQ, zero pre-ring cost. Reduces peak but not T60. |
| `skip` | Peak < +3 dB or T60 already at target | Don't waste tap budget. |

A SafetyValidator runs ahead of every write. SVS PB12-NSD profile caps
boost at +6 dB per band, +9 dB cumulative per 1/3-octave, +20 dB
intent="modal_cancel" peak. The validator enforces these in code, not
in prompts — so even if Claude designs an aggressive FIR the AVR
hardware never sees a coefficient set that would damage a driver.

The loop converges. With cs=0.3 and a Gabor anti-pulse on the 47 Hz
mode of my room (T60 was ~700 ms before), T60 drops to ~390 ms post-FIR
— ~45% reduction. Magnitude correction at the same band is ~4 dB at
the listener. Compare a pure PEQ cut at 47 Hz: it might say "-6 dB" on
the input, but at the listener you measure ~1-2 dB of attenuation and
the room still rings 600+ ms. **PEQ cannot suppress modal ringing**;
this is now well understood and lives as a memory note that Claude
references.

The whole loop is in version control — recipes are markdown files in
`recipes/core/`, tools are typed MCP endpoints, and the MCP server runs
in a Docker container on the Pi. Claude reads the recipe, makes tool
calls, and the calibration runs end-to-end without me touching anything
once it starts.

## 3. The Denon write path: bypassing the MultEQ Editor app

This is the new bit, finished tonight, and it's where things get
interesting if you have a Denon/Marantz with Audyssey MultEQ XT32.

The Audyssey calibration pipeline is normally:
1. You run `mic-on-tripod` Audyssey calibration via the MultEQ Editor
   mobile app — this measures impulse responses at each position.
2. The app derives a per-channel FIR + per-channel distance/level from
   those measurements.
3. The app writes the result back to the AVR by pressing "Send to AV
   receiver".

OCA's A1 Evo / Acoustica scripts let you intercept step 2: edit the .ady
file on a PC, recompute filters from the IRs in REW, push the modified
.ady back via the app. That's a meaningful improvement — but step 3 still
goes through the proprietary mobile app.

What I built tonight closes that loop. The AVR speaks a fairly
straightforward TCP protocol on port 1256. Two findings made everything
else possible:

### Finding 1: the variance-cap bypass

Sending a `SET_SETDAT {"Distance": [...]}` payload alone hits the AVR's
firmware variance cap (~38 ms of applied delay between channels — matches
the UI's 18 m / 60 ft clamp). It doesn't matter what number you push —
the firmware re-validates on EXIT_AUDMD and snaps back to the cap.

But sending `{"Distance": [...], "AudyFinFlg": "NotFin"}` followed by a
separate `{"AudyFinFlg": "Fin"}` commit packet tells the firmware "this
is a complete calibration write, not a partial poke." The larger Distance
values stick. New ceiling: ~55 ms applied delay (still capped, but ~17 ms
more than before).

In my room this was the difference between 7 ms of residual sub-vs-mains
misalignment and 0 cancelling bands across 40-120 Hz.

The "rumored CustomDistance trick" is a red herring: there's no
CustomDistance field on the wire. It only exists in the .ady JSON file
that the app reads — the app translates customDistance → distance
before sending. The actual mechanism is the NotFin/Fin commit dance.

### Finding 2: the filter wire format

The AVR doesn't accept biquads — it accepts a multi-rate FIR tap stream:
* 16,321 input taps per speaker, polyphase-decimated 4× to 1024 output taps
* 16,055 input taps per sub, polyphase-decimated 4× to 704 output taps

Each filter is shipped 6× per channel: 2 target curves (Flat + Reference,
both stored in the AVR's flash for runtime toggling via AudyEqSet) ×
3 sample rates (32 / 44.1 / 48 kHz). For my 9.1 setup that's ~522
binary packets per upload. Each is a 531-byte SET_COEFDT frame with a
4-byte stream header and 126 little-endian float32 coefficients.

The full upload sequence:

```
ENTER_AUDY                                              → ACK
SET_SETDAT (chunked at 510 B, 16 ordered fields,        → ACK each
            AudyFinFlg=NotFin in the first chunk)
[INIT_COEFS — only if DType startsWith "fixed"]         → ACK
for each channel:
    for each (target_curve, sample_rate):
        SET_COEFDT × N packets (no ACK, fire-and-forget)
        sleep CoefWaitTime.Init
    sleep ~20 ms
sleep CoefWaitTime.Final                                # 15 s on X3800H
FINZ_COEFS                                              → ACK
SET_SETDAT {"AudyFinFlg":"Fin"}                         → ACK   ← commit
EXIT_AUDMD                                              → ACK
```

I wrote a clean Python implementation that handles all of this, with
attribution to `srinivas486/audyssey-rew-tuner` for the polyphase
decimation math (MIT-licensed, clean-room port of A1Evo's transfer.js).
Two MCP tools wrap it:

```python
# Claude calls these as part of a recipe
design_avr_fir(channel_id="FL", target_curve_db=[
    {"freq_hz": 30, "gain_db": +2},
    {"freq_hz": 60, "gain_db": -3},   # cut 60 Hz mode
    {"freq_hz": 200, "gain_db": 0},
], cache_key="iter-1")

# ...one design_avr_fir call per channel...

apply_avr_fir(host="192.168.1.209",
              ady_path="/storage/state.ady",
              cache_key="iter-1",
              distances_override_m={"SW1": 20.0})  # also bypass the cap
```

The full envelope (16 ordered fields with proper types — booleans not
strings, integers not strings, capital-Q in `AudyMultEQ`) gets pushed
together with the coefficient streams. That avoids the FR-drift side
effect that bit me when I tried a minimal 2-field envelope earlier
(mids dropped 7-9 dB because the AVR applied defaults for the
unspecified Audy* EQ fields on Fin commit).

End-to-end smoke test against my X3800H tonight:
- AVR responded to ENTER_AUDY/GET_AVRINF/GET_AVRSTS
- 1619-byte envelope chunked cleanly into 5 sub-510B SET_SETDAT packets
- 522 SET_COEFDT packets generated (9 speakers × 54 + 1 sub × 36)
- All without playing a single sweep

The full transmit path is gated on a typed confirmation in the smoke
script — overwriting MultEQ filter banks is not an action to take
casually. To recover, push the original .ady via the official MultEQ
Editor app (or back through this same tool with the original IRs).

## Putting it all together

Now that all three pieces exist, the full flow looks like:

1. Claude reads `recipes/core/full-room-calibration-fir.md` (next step
   on the list)
2. Per channel: `measure → analyze_decay → analyze_phase → design
   intents → simulate_eq → design_avr_fir`
3. Once all channels are designed: `apply_avr_fir` pushes everything in
   one TCP session
4. Re-measure → check convergence → iterate or stop

The AVR is no longer the inscrutable Audyssey black box. It's a
filter-bank target the LLM can write to.

## What's next

* The CamillaDSP `hw:` + 48 kHz native migration (saves ~15-25 ms of
  sub-chain latency, matches the long FIR's design rate to the device
  rate)
* A full mains calibration recipe driving Claude through the per-channel
  measure→analyze→design→apply→verify loop
* Multi-position averaging using all the .ady's stored impulse
  responses, not just MLP

If anyone here has done similar work — especially the OCA / A1 Evo
folks whose research was foundational — I'd love to compare notes.
The repo is open source (MIT-licensed, attribution preserved); PRs and
issues welcome.

GitHub: https://github.com/abarbaccia/avr-calibration

---

*Hardware: Pi 5, Focusrite Scarlett 18i20, Denon X3800H, miniDSP UMIK-1,
2× SVS PB12-NSD, Chane A2.4 fronts, Polk VT60 in-ceiling.*

*Software: CamillaDSP, denonavr, ratbuddyssey-style TCP, sounddevice +
PyTTa for measurement, scipy + numpy + custom polyphase math for FIR
design, Claude Code + MCP for orchestration.*

*References cribbed with attribution: cepage/A1EvoAcoustica,
srinivas486/audyssey-rew-tuner, BRNKR/audyssey_one,
ratbuddy/ratbuddyssey, AVS Forum threads.*

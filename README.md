# avr-calibration

A PhD acoustic engineer in your listening room — powered by an LLM, fed by a digital twin of your hardware and room, guided by AV community recipes and best practices.

It's not a batch process — it's a conversation with something that understands your room.

```
> help me calibrate the room and improve the acoustics

  A few questions first. What are you optimizing for — music,
  movies, or both? Harman bass target adds a gentle low-end shelf
  that most people prefer for cinema. Cuts-only is more conservative
  and works well for music purists. I can also do a flat target
  if you just want accuracy. What sounds right?

> movies mostly. let's do Harman

  Running bass-calibration recipe with Harman target. Phase 1:
  time-aligning subs… Sub 2 arrives 2.3 ms early — applying
  2.3 ms delay. Phase 2: per-sub EQ. 38 Hz mode is +9 dB on
  sub 1, minimum-phase — cutting with PEQ. 62 Hz null is
  cancellation, skipping…

> the bass sounds muddy and I'm missing the "thump" at loud volumes

  Two things going on. The 38 Hz mode rings for 1200 ms — that
  smears transients and makes everything sound muddy. A 2" rigid
  fiberglass panel in the front corner would cut that in half.
  I can also tighten it with a FIR filter right now — want me to
  design one and A/B it? Second: you're losing punch at loud
  volumes. Let me run a headroom test — I'll sweep at -15, -12,
  and -9 dB and check for clipping or compression in the amp.
  That'll tell us if it's the room or the hardware.

> what are the biggest problem areas in my room?

  Three things, ranked by impact:
  1. 38 Hz mode (+9 dB, T60 of 1200 ms) — this is the boom you
     hear on explosions. FIR can cut the ringing time in half.
  2. 65 Hz null (-14 dB) — cancellation between your two subs.
     EQ can't fix this. Moving sub 2 off the side wall midpoint
     would fill it.
  3. 23-28 Hz rolloff — you're 6 dB below Harman target here.
     Port tuning limits the sub below 22 Hz, so this is close to
     the physical limit. A sealed sub or larger ported sub would
     extend it. Want me to measure each problem in detail?

> should I put my second sub nearfield behind the couch?

  Maybe — let's measure instead of guess. Sub crawl: put the sub
  at the listening position, I measure from 3-4 candidate spots.
  Takes about 10 minutes. The smoothest response wins. Ready?

> do I need a bigger amp?

  No. Your calibrated level is -15 dB, Denon clips at -8 dB —
  7 dB of headroom. The 65 Hz null costing you 12 dB is cancellation
  between subs. That's placement, not power.

> would adding a third sub help?

  It would, but you'd get 80% of the benefit for free first.
  The 65 Hz null is your biggest problem — it's cancellation
  between your two subs at the side wall midpoint. Moving sub 2
  eighteen inches toward the corner fills that null without buying
  anything. Want me to run a sub crawl to find the optimal spot?
  After that we can talk about whether a third sub is still worth it.
```

## Five layers

```
 ════════════ PHILOSOPHY ════════════
 Recipes — plain English calibration approaches.
 Harman bass, cinema BEQ, cuts-only purist,
 multi-seat averaging. Community knowledge
 you can read, fork, and contribute to.
            │
            │  guides decisions
            ▼
 ════════════ INTELLIGENCE ════════════
 Claude Code — executes the recipe, reasons
 about your room, designs filters, recommends
 physical changes. All decisions live here.
            │
            │  MCP tool calls
            ▼
 ════════════ TOOLS ════════════
 MCP server — measurement, analytics, and
 simulation. Phase decomposition, coherence,
 FIR design, EQ simulation, safety validation.
 Data and math — no decisions.
            │
            │  plugin drivers
            ▼
 ════════════ HARDWARE ════════════
 Protocol drivers — Denon AVR (denonavr),
 miniDSP 2x4 HD (minidsp-rs CLI), UMIK mic
 (PyTTa). Hardware I/O and sequencing.
            │
            │  signal path
            ▼
 ════════════ PHYSICAL ════════════
 Your room — subs, speakers, treatments.
 What the system measures and what the best
 recommendations usually change.
```

**Philosophy** — [Recipes](recipes/core/) are human-readable markdown that encode a calibration approach. The [bass calibration](recipes/core/bass-calibration.md) recipe runs five phases: time-align subs, flatten per-sub response, reduce ringing with FIR, shape to a [target curve](recipes/curves/), then a retrospective with physical recommendations ranked by impact. Anyone can [write a recipe](recipes/TEMPLATE.md) — different philosophy, same measurement rigor and safety guarantees.

**Intelligence** — the LLM reads the recipe and drives a closed loop: measure, decompose into fixable vs unfixable, simulate corrections, apply, re-measure, converge. It reasons about what's outside DSP too — sub placement, room treatment, hardware limits — because those are usually higher-impact than another filter.

**Tools** — MCP tools provide data and simulation; the LLM provides judgment. `measure` takes a sweep; `analyze_phase` decomposes into fixable vs cancellation; `simulate_eq` predicts the result of a proposed filter set; `design_fir` computes coefficients for time-domain corrections. A `SafetyValidator` enforces hard limits on every DSP write in code, not prompts.

**Hardware** — plugin drivers that speak hardware protocols. Each driver owns the sequencing and error handling for one device — Denon sweep context (set input, play, restore), minidsp-rs CLI (PEQ writes with 300ms pacing), PyTTa (sweep/deconvolution with UMIK cal). Adding hardware means writing a driver, not changing anything above.

**Physical** — the room and everything in it. No amount of EQ fixes a cancellation null. The system's most impactful recommendations usually live here.

## The digital twin

Every measurement, filter decision, and outcome is captured. Across sessions, the system accumulates a model of your specific room: which modes respond to FIR, which nulls are placement problems, where your amp clips, what positions have been tried.

The second calibration is better than the first. After you move a sub on its recommendation, it already knows your room's mode structure and starts from better assumptions. After you add a bass trap, it knows which mode to recheck. It's building a cumulative understanding that a fresh-start tool never has.

## Supported hardware

| Component | Supported | Role |
|-----------|-----------|------|
| Intelligence | Claude Code | Orchestration, reasoning, filter design |
| AVR | Denon / Marantz | Volume, input, sweep playback |
| DSP | miniDSP 2x4 HD | PEQ, FIR, delay, routing |
| Mic | UMIK-1 / UMIK-2 | Measurement (USB) |
| Compute | Raspberry Pi 5 | Headless service |

Plugin-based — each driver is independent. Adding hardware means writing a driver, not changing calibration logic.

## Quick start

Open [Claude Code](https://claude.ai/claude-code) and paste:

```
Help me set up avr-calibration: https://github.com/abarbaccia/avr-calibration
```

Claude reads the [setup guide](docs/setup-guide.md), asks what hardware you have, figures out where to run the service, deploys it, and verifies the connection. The whole setup is a conversation — no assumptions about what you already have.

Once connected:

```
> calibrate the subs to Harman bass target
```

## Contributing

The most valuable contribution is a recipe. If you have a calibration philosophy — cuts-only, cinema bass maximalist, multi-seat averaging, "pre-EQ the subs before Audyssey" — write it up in plain English and open a PR. The system executes it with the same measurement pipeline and safety guarantees. See [`recipes/TEMPLATE.md`](recipes/TEMPLATE.md).

Other ways to contribute:
- **Hardware drivers** — support for new AVRs, DSPs, or mics
- **Target curves** — add a JSON file to [`recipes/curves/`](recipes/curves/)
- **Bug reports** — especially around hardware edge cases (every AVR is different)

```bash
uv venv .venv && source .venv/bin/activate && uv sync --extra dev
uv run python -m pytest tests/ -v
```

## Support the project

If this saved you hours of manual calibration or helped you understand your room, consider supporting development:

[GitHub Sponsors →](https://github.com/sponsors/abarbaccia)

The project is and will remain open source. Your support helps fund hardware testing (every AVR and DSP model behaves differently) and LLM tokens for writing new code.

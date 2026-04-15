# avr-calibration

A PhD acoustic engineer in your listening room — powered by an LLM, fed by a digital twin of your hardware and room.

This system builds a living model of your space: hardware setup, signal chain, room modes, decay times, sub interactions, phase behavior, hardware limits. Then it puts an LLM in the role of calibration engineer — one that reads that model, applies DSP corrections for what EQ can fix, and tells you what to physically change about the rest.

### Give it feedback

It's not a batch process. It's a conversation with something that understands your room.

```
> the bass sounds boomy on action movies but music is fine

  Your cinema bass curve boosts +10 dB at 20 Hz. That's hitting
  the 23 Hz room mode hard — T60 is 1528 ms, so explosions
  excite it and it rings for over a second. Two options: switch
  to Harman bass (+6 dB, gentler slope) or I can add a narrow
  FIR correction at 23 Hz. Want me to simulate both and compare?
```

### Ask it hard questions

```
> should I put my second sub nearfield behind the couch?

  Maybe — but let's measure instead of guess. I'll run a sub
  crawl: put the sub at the listening position, then I'll measure
  from 3-4 candidate spots (front wall, side wall, nearfield,
  current position). Takes about 10 minutes. The smoothest
  response wins. Ready?

> do I need a bigger amp?

  No. The Denon clips at -8 dB all-channels-driven, but your
  calibrated level is -15 dB — 7 dB of headroom. The 65 Hz null
  costing you 12 dB is cancellation between subs. That's placement,
  not power. Moving sub 2 off the wall midpoint fixes it for free.

> what would a third sub actually do for me?

  Fill the 65 Hz null (quarter-wavelength offset from the side
  wall) and smooth seat-to-seat variance by 4-6 dB. Biggest
  upgrade after repositioning sub 2. More impactful than any DSP
  change at this point.
```

## Four layers

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
 ════════════ HARDWARE ════════════
 avr-calibration service (Pi, Docker)
 MCP tools, safety validator, AVR/DSP/mic
 drivers. Data and simulation — no decisions.
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

**Hardware** — MCP tools provide data and simulation; the LLM provides judgment. A `SafetyValidator` enforces hard limits on every DSP write in code, not prompts. Plugin architecture — adding hardware means writing a driver, not changing anything above.

**Physical** — the room and everything in it. No amount of EQ fixes a cancellation null. The system's most impactful recommendations usually live here.

## The digital twin

Every measurement, filter decision, and outcome is captured. Across sessions, the system accumulates a model of your specific room: which modes respond to FIR, which nulls are placement problems, where your amp clips, what positions have been tried.

The second calibration is better than the first. After you move a sub on its recommendation, it already knows your room's mode structure and starts from better assumptions. After you add a bass trap, it knows which mode to recheck. It's building a cumulative understanding that a fresh-start tool never has.

## Supported hardware

| Component | Supported | Role |
|-----------|-----------|------|
| AVR | Denon / Marantz | Volume, input, sweep playback |
| DSP | miniDSP 2x4 HD | PEQ, FIR, delay, routing |
| Mic | UMIK-1 / UMIK-2 | Measurement (USB) |
| Compute | Raspberry Pi 5 | Headless service |

Plugin-based — each driver is independent. Adding hardware means writing a driver, not changing calibration logic.

## Quick start

```bash
# 1. Deploy to Pi
bash <(curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh)

# 2. Edit config with your hardware
nano /home/pi/.avr-calibration/config.yaml

# 3. Add MCP server to Claude Code (.claude/mcp.json)
# { "mcpServers": { "avr-calibration": { "type": "sse", "url": "http://<pi-ip>:8765/sse" } } }

# 4. Calibrate
# > calibrate the subs to Harman bass target
```

[Full setup guide →](docs/mcp-setup.md)

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

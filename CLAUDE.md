# avr-calibration

AI-first home theater calibration — closed-loop bass optimization for Denon X3800H + miniDSP 2x4 HD + SVS PB12-NSD.

## Architecture — Claude is the Orchestrator

**Core principle: Claude Code drives calibration, not Python.**

Python provides MCP tool primitives (measure, apply EQ, mute/unmute, set delay).
Claude reads human-readable recipe files and calls those tools in a loop.

**Do NOT build Python orchestrators, phase runners, or loop state machines.**
If you're writing a `for` loop in Python that calls measure→analyze→apply→repeat,
STOP — that logic belongs in a Claude skill or recipe, not in Python code.

```
[ Claude Code ]  ←── reads recipe, drives calibration loop
       |
       | MCP tool calls (high-level actions)
       ▼
[ Pi 5 @ 192.168.1.117 — Docker (arm64) ]
  ├── MCP server (thin facade)
  │     ├── measure             take a sweep, return FR data
  │     ├── apply_eq            write PEQ filters (SafetyValidator enforced)
  │     ├── mute/unmute_output  per-output muting for solo measurement
  │     ├── set_delay           per-output delay for time alignment
  │     ├── set_polarity        per-output polarity inversion
  │     ├── get_state           combined hardware state
  │     ├── check_system        preflight all hardware
  │     └── fetch_recipe        load recipe text
  │           |
  │     Plugin drivers (Claude never sees these directly)
  │     ├── DenonDriver         AVR volume, input, sweep context
  │     ├── MinidspDriver       DSP EQ, mute, delay, polarity
  │     ├── MeasurementEngine   UMIK + PyTTa sweep/deconvolution
  │     └── SafetyValidator     hard limits, NEVER bypassed
  ├── Web UI            ← browser dashboard (read-only)
  └── SessionStore      ← SQLite history
```

### What belongs where

| Concern | Where | Example |
|---------|-------|---------|
| Loop logic (measure→analyze→adjust→repeat) | Claude skill / recipe | `/avr:calibrate` reads recipe, calls MCP tools |
| Decision-making (what to adjust next) | Claude | "subs are 3ms apart, increase delay on sub 1" |
| Filter design (which freqs, PEQ vs FIR) | Claude | "45Hz is min-phase, cut it; 55Hz is cancellation, skip" |
| Numerical computation (FFT, biquad math) | MCP tools | `simulate_eq`, `optimize_q`, `design_fir` |
| Analytics (phase, coherence, fixability) | MCP tools | `analyze_phase`, coherence in FR data |
| Hardware protocol (ordering, cleanup) | Plugin drivers | DenonSweepContext sets input before play, restores after |
| Hardware I/O | MCP tools → plugin drivers | `measure` → DenonDriver + MeasurementEngine + UMIK |
| Safety enforcement | SafetyValidator (code) | Max boost, HPF, frequency limits — NEVER in prompts only |
| Recipes | Markdown files in `recipes/core/` | Human-readable English instructions |
| Hardware config | `config.yaml` | Output slot types, IP addresses, mic name |

### MCP tool design principle
Claude sees **actions** ("measure", "apply EQ"), not **hardware** ("set Denon input",
"POST to minidsp-rs"). The MCP server is a thin facade. Each tool handler is ~5 lines
delegating to a plugin driver. Hardware protocol complexity stays in the drivers.

### LLM-first tool design — HARD RULE

**Tools provide DATA and SIMULATION. The LLM provides JUDGMENT.**

Never build deterministic solvers for decisions the LLM should make. If a tool
contains `for` loops that decide *what* to correct, *where* to place filters, or
*which* frequencies to target — STOP. That decision belongs to the LLM.

Tools the LLM needs:
- **Analytics** — compute derived data the LLM can't (FFT, min-phase decomposition,
  coherence, FIR coefficients). Return the results; don't interpret them.
- **Simulation** — "if I apply these filters, what would the FR look like?" Pure math.
- **Optimization** — "I chose 45Hz/-5dB, what Q minimizes error?" Numerical search.
- **Hardware I/O** — measure, apply EQ, set delay. Execute what the LLM decided.

Tools the LLM does NOT need:
- **Solvers** — "given this FR, suggest filters." That's the LLM's job.
- **Optimizers that choose targets** — "find the best reference level." LLM decides.
- **Auto-anything** — if it makes a calibration decision, it belongs in the recipe/LLM.

The LLM's advantage over REW/Dirac is *contextual judgment*: it sees decay data,
phase data, coherence, cross-sub interaction, user constraints, and room history
simultaneously. A deterministic solver sees one number and optimizes it. Don't
replace the LLM's judgment with a greedy algorithm.

```
WRONG:  measure → suggest_filters() → apply     (Python decides)
RIGHT:  measure → analyze_phase() → LLM reasons → simulate_eq() → LLM adjusts → apply
```

## Hardware

- **AVR:** Denon X3800H (denonavr library)
- **DSP:** miniDSP 2x4 HD (minidsp-rs daemon → HTTP)
- **Mic:** UMIK-1 or UMIK-2 (UMIK .cal correction applied)
- **Measurement:** PyTTa (log sweep + deconvolution)
- **Subs:** SVS PB12-NSD (ported, ~22Hz tuning)

## Safety Limits (SVS PB12-NSD)

These are enforced in `SafetyValidator` before any write to miniDSP:
- Minimum boost frequency: **25Hz**
- Max boost per EQ band: **+6 dB**
- Max cumulative boost in any 1/3 octave: **+9 dB**
- Max change per iteration: **+3 dB/band**
- Mandatory infrasonic HPF: **18Hz, 4th-order Butterworth** (always on)
- Cuts: no floor (cuts are always safe)

## Development

```bash
# Set up environment
uv venv .venv && source .venv/bin/activate
uv sync --extra dev

# Run tests
uv run python -m pytest tests/ -v

# Run the CLI
calibrate --help
calibrate check
calibrate measure [--label TEXT]
```

## Testing

100% test coverage is the goal — tests make vibe coding safe.

- Run: `pytest tests/ -v`
- Test files: `tests/test_*.py`
- See `TESTING.md` for conventions

When writing new functions, write a corresponding test.
When fixing a bug, write a regression test.
When adding error handling, write a test that triggers the error.
When adding a conditional, write tests for BOTH branches.
Never commit code that makes existing tests fail.

## Deployment

- Docker image built by GitHub Actions on every branch push
- Branch push → `ghcr.io/abarbaccia/avr-calibration:<branch-name>`
- Main push → also tagged `:latest`
- arm64 cross-compiled in CI; no compilation on the Pi
- Source installed at `/opt/venv/lib/python3.11/site-packages/calibrate/` inside container
- Pi 5 at `192.168.1.117` (user `pi`)

**Primary workflow:** hotfix first, pipeline second.

```
SSH hotfix → validate → git push → CI build → pull latest image → validate → merge
```

**SSH hotfix (seconds, no rebuild):**
```bash
./deploy/hotfix.sh                    # auto-detects modified calibrate/ files
./deploy/hotfix.sh calibrate/web.py   # specific file
```

**Pull latest image after CI build completes:**
```bash
ssh pi@192.168.1.117 "sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest && sudo systemctl restart avr-calibration"
```

## Calibration Knowledge (for Claude driving calibration)

### Signal chain
Pi (sweep via HDMI LFE) → Denon X3800H → miniDSP 2x4 HD → subs/shakers → room → UMIK mic → Pi (recording)

### How to interpret frequency response
- **Room modes**: Large peaks/nulls in 30-80Hz are room modes. Cut peaks (always safe). Nulls cannot be filled with EQ — they're cancellation.
- **Port tuning**: SVS PB12-NSD tuned ~22Hz. Output rolls off steeply below port frequency. Do not boost below 25Hz.
- **Fixable with EQ**: Broad humps, gentle slopes, peaks from room modes (cut them)
- **NOT fixable with EQ**: Deep nulls (cancellation), frequencies below sub capability, anything above sub crossover

### Data-driven decision making
Use the analytics pipeline to inform every EQ/FIR decision:
- **`analyze_phase`**: Check fixability before designing any filter. Minimum-phase errors are correctable; excess-phase errors (cancellation) are not. Don't waste a PEQ slot on an unfixable problem.
- **Coherence** (in FR data): Low coherence (<0.8) means the measurement is unreliable at that frequency. Don't design precise corrections based on noisy data.
- **`simulate_eq`**: Verify every proposed filter set before applying to hardware. Iterate in simulation until satisfied, then apply once.
- **`compare_sub_phase`**: Before alignment, check per-frequency phase relationship between subs. Know where they reinforce vs cancel before deciding on delay/polarity.
- **`design_fir`**: For time-domain problems (long T60 decay), FIR shortens the ringing. PEQ only reduces the peak — the mode still rings. Use `analyze_decay` to identify candidates, then `design_fir` to compute coefficients.

### Sub alignment procedure
1. Mute all subs except one. Measure. Repeat for each sub.
2. Compare IR peak times — the difference is the travel-time delay between subs.
3. Apply delay to earlier-arriving subs so all peaks align.
4. Check polarity — if one sub's IR peak is inverted relative to others, flip it.
5. Level-match — adjust gains so all subs have equal SPL at the mic.
6. Re-measure to verify alignment. Repeat if needed.

### Sub crawl procedure
1. Place the sub at the primary listening position (on/near the seat).
2. Place the mic at each candidate sub position.
3. Measure at each position. Compare FR smoothness across 20-80Hz.
4. Choose the position with the smoothest response (fewest/shallowest nulls).
5. For multiple subs, crawl each independently, then measure combined.

### Harman bass target (relative to 80Hz reference)
| Hz  | Target |
|-----|--------|
| 25  | +5 dB  |
| 31  | +4 dB  |
| 40  | +3 dB  |
| 50  | +2 dB  |
| 63  | +1 dB  |
| 80  | 0 dB   |

## Safety Limits (SVS PB12-NSD) — Code-Enforced, Non-Negotiable

These are enforced in `SafetyValidator` before any write to miniDSP.
They exist in Python code, not just in prompts. Never bypass them.

- Minimum boost frequency: **25Hz**
- Max boost per EQ band: **+6 dB**
- Max cumulative boost in any 1/3 octave: **+9 dB**
- Max change per iteration: **+3 dB/band**
- Mandatory infrasonic HPF: **18Hz, 4th-order Butterworth** (always on)
- Cuts: no floor (cuts are always safe)

## Key design decisions

- **Claude Code is the orchestrator** — reads recipes, drives calibration loop, makes decisions
- **LLM designs filters, tools do math** — never build deterministic solvers for decisions the LLM should make. Tools provide analytics (phase, coherence, fixability) and simulation (predicted FR). The LLM reasons about the full context and decides what to correct, where, and how.
- **Python provides MCP primitives** — measure, apply EQ, mute/unmute, set delay (no orchestration)
- **Rich analytics pipeline** — mic-corrected FR, minimum-phase decomposition, coherence, group delay, cross-sub phase analysis. The LLM's judgment is only as good as the data it sees.
- **Recipes are English markdown** — human-readable instructions in `recipes/core/`
- **PyTTa** replaces REW as the measurement engine (free, sufficient for bass calibration)
- **minidsp-rs** daemon handles USB control of the 2x4 HD; Python speaks HTTP to it
- **denonavr** library handles Denon X3800H control
- **SQLite** for measurement history storage
- **Harman target curve** as the default optimization target

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- "Calibrate", "run calibration", "tune the subs" → invoke avr:calibrate
- "Verify", "check integration", "how's it all sound", "post-Audyssey" → invoke avr:calibrate (full-room-verify recipe)
- "Set up", "configure", "new hardware" → invoke avr:configure
- "Build a recipe", "new recipe", "full room recipe" → invoke avr:recipe
- "Sub crawl", "find best position" → invoke avr:calibrate (sub-crawl recipe when available)
- "Take a measurement", "how does it sound" → invoke avr:measure
- "Check system", "is everything connected" → invoke avr:check
- "What's the current state", "where are we" → invoke avr:status
- Bugs, errors, "why is this broken" → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- Code review, check my diff → invoke review
- Architecture review → invoke plan-eng-review

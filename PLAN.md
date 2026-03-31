<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/main-autoplan-restore-20260331-004220.md -->

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open | 7 findings; data model changed from graph to flat config (APPROVED by user); premise gate passed |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | issues_open | 12 findings; all auto-decided and added to plan; empty state, picker, save model, load state all specified |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open | 9 findings; 1 CRITICAL (sub_outputs sync); all auto-decided; test plan written to disk |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | UNAVAILABLE | Codex returned unavailable for all 3 phases |

**VERDICT:** REVIEWED [subagent-only, Codex unavailable]. Critical fix required in implementation: `POST /api/signal-chain` MUST write `measurement.sub_outputs` from non-empty output slots or the calibration loop is disconnected from the new UI. All other issues are auto-decided and documented. Plan is implementation-ready.

# Plan: Signal Path Builder

**Branch:** feat/signal-path-builder
**Base:** feat/equipment-setup
**Feature Brief:** ~/.gstack/projects/abarbaccia-avr-calibration/andrew-feat-signal-path-builder-feature-20260331-0039.md

## What We're Building

Replace the three-card flat equipment setup UI (Denon / miniDSP / Speakers as independent CRUD forms) with a **vertical signal-chain builder** that traces the audio path from the Pi outward, node by node.

**Pi root node:** Displayed as a small muted non-card header above the chain (not a full card). Labeled "Calibration Host" with a tooltip explaining measurement-only role. Connectivity status shown as a small green/red dot only.

The user sees:

```
  ┌──────────────────────────────────┐
  │  Calibration Host (Pi)  ● Online │   ← small header, not a full card
  └──────────────────┬───────────────┘
                     │ HDMI out
┌────────────────────▼─────────────────┐
│  Denon X3800H                  [OK]  │   ← full card, always expanded
│  Host: 192.168.1.100  [Discover]     │
│  Pi input:  [HDMI 1 ▾]  [Test]       │
│  Sub output: [Sub Out ▾]             │
│                          [Save]      │
└────────────────────┬─────────────────┘
                     │ Sub Out
┌────────────────────▼─────────────────┐
│  miniDSP 2x4 HD                [OK]  │   ← full card, always expanded
│  In 1: [LFE L      ]  In 2: [LFE R] │
│  Out 1 → [Sub L  front-left   ✕]    │
│  Out 2 → [Sub R  front-right  ✕]    │
│  Out 3 → [+ Add speaker]            │
│  Out 4 → [+ Add speaker]            │
│                    [Save Changes]    │
└──────────────────────────────────────┘

[Save Chain]   ← teal CTA at bottom
```

**EMPTY STATE (new user, no config):**
```
  ┌──────────────────────────────────┐
  │  Calibration Host (Pi)  ● Online │
  └──────────────────┬───────────────┘
                     │
┌────────────────────▼─────────────────┐
│  Denon X3800H                  [ — ] │
│  ┌─────────────────────────────────┐ │
│  │  Click Discover to find your    │ │
│  │  Denon AVR on the network       │ │
│  └─────────────────────────────────┘ │
│  [Discover]  or enter IP manually:   │
│  [________________]  [Save]          │
└──────────────────────────────────────┘
[+ Add DSP]   ← greyed out until Denon is saved
```

**PARTIAL STATE (Denon saved, DSP not yet configured):**
```
┌─────────────────────────────────────────┐
│  Denon X3800H                    [OK]   │
│  ...configured...                        │
└─────────────────────────┬───────────────┘
                          │ Sub Out
┌─────────────────────────▼───────────────┐
│  miniDSP 2x4 HD                  [ — ] │
│  ┌──────────────────────────────────┐   │
│  │  No DSP configured yet.          │   │
│  │  Host defaults to localhost:5380 │   │
│  └──────────────────────────────────┘   │
│  [Configure DSP]                         │
└─────────────────────────────────────────┘
```

**OFFLINE STATE (Denon unreachable):**
```
│  Denon X3800H                  [FAIL]  │
│  Host: 192.168.1.100 ← amber highlight │
│  ⚠ Cannot reach Denon (timeout 5s)     │
│  [Retry]  [Change host]                 │
```

**LOADING STATE:** Skeleton cards with `.pending` badge animation during `initChainBuilder()`. Each node card renders immediately in skeleton state (grey placeholders for text, `.running` badge) while the fetch is in progress.

**SAVE MODEL:** Explicit "Save Chain" button at bottom of the page. No auto-save. Per-section save buttons kept inside Denon and miniDSP cards for individual changes (consistent with existing pattern). "Save Chain" saves the full topology.

**SETUP GATE:** Step 2 ("Baseline") unlocks when: Denon host is saved AND at least one output slot has a speaker configured. Gate checked on each save.

**"+ Add speaker" INLINE PICKER:** Clicking "+ Add speaker" on an output slot expands that row inline:
```
Out 3 → ┌──────────────────────────────────┐
         │ Label:    [________________]    │
         │ Location: [front-left      ▾]   │
         │ Preset:   [SVS PB12-NSD    ▾]   │
         │           [Save]  [Cancel]      │
         └──────────────────────────────────┘
```
Cancel collapses without saving. Save adds the speaker and shows slot row.

**NODE EXPAND/COLLAPSE:** Always expanded in v1. No accordion. Explicitly out of scope.

**LOCATION SOURCE OF TRUTH:** `output_slots[].location` in config.yaml is authoritative. Speaker `equipment.data` blob does NOT store `room_location`. The measurement loop looks up location via `minidsp.output_slots[i].location`. Speaker records in SQLite are identified by their slot index.

The user sees:

```
┌─────────────────────┐
│  Pi (AVR Calibration)│   (root — always present)
└──────────┬──────────┘
           │ HDMI
┌──────────▼──────────┐
│  Denon X3800H  [OK] │   ← host, input selection, sub out picker
│  Input: HDMI 1      │
│  Sub Out →          │
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  miniDSP 2x4 HD [OK]│   ← input labels, 4 output slots
│  In: LFE L / LFE R  │
│  Out 1 → [Speaker]  │
│  Out 2 → [Speaker]  │
│  Out 3 → [+ Add]    │
│  Out 4 → [+ Add]    │
└─────────────────────┘
```

Each node shows a live connectivity badge. Each output slot has a "+ Add" affordance: add a speaker (with preset selector and room location) or another device.

## Data Model (Approach C — Extended Flat Config)

*CEO review decision: graph model over-engineered for fixed linear topology. Use extended flat config.*

### config.yaml additions
```yaml
minidsp:
  host: "localhost"
  port: 5380
  input_labels:            # existing field, renamed from connections.minidsp.inputs
    "0": "LFE L"
    "1": "LFE R"
  output_slots:            # NEW: replaces output labels, adds location + preset
    - index: 0
      label: "Sub L"
      location: "front-left"
      preset: "pb12-nsd"
    - index: 1
      label: "Sub R"
      location: "front-right"
      preset: "pb12-nsd"
    - index: 2
      label: ""
      location: ""
      preset: ""
    - index: 3
      label: ""
      location: ""
      preset: ""
```

Speaker `room_location` added to `equipment.data` JSON blob (no DB migration — already an open blob).

### Speaker presets (v1)
- `pb12-nsd`: SVS PB-12 NSD, 12" ported, ~22Hz tuning
- Room locations: `front-left`, `front-right`, `rear-left`, `rear-right`, `center`, `other`

## Backend Changes

### New endpoints
- `GET /api/signal-chain` — synthesize chain from flat config + speakers (computed, not stored)
- `POST /api/signal-chain` — write to `denon.*`, `minidsp.input_labels`, `minidsp.output_slots`, speakers table

### Existing endpoints (kept, not changed)
- `/api/equipment/denon/*` — unchanged
- `/api/equipment/minidsp/save-labels` — keep for backward compat, deprecated in UI
- `/api/equipment/speakers` — keep as-is

### config.py changes
- Add `output_slots` to minidsp section defaults
- `Config.minidsp` now includes `output_slots` list

## Frontend Changes

### Replace Phase 1 (Equipment Setup) HTML
- Remove the 3-card layout (Denon card, miniDSP card, Speakers card)
- Replace with chain builder: vertical list of node cards, each card renders based on node type
- Dynamic: "Pi" root is always first. Each node's outputs render as slots. Clicking "+ Add" on a slot opens an inline picker (device type or speaker).
- Connectivity badges: each device card has a status indicator that polls `/api/preflight/denon` or `/api/preflight/minidsp-combined` on load

### JavaScript

In-memory chain state (`_chainState` object) is the single JS source of truth. All saves (per-card and "Save Chain") read from and write back to `_chainState`.

- `initChainBuilder()` — GET `/api/signal-chain`, populate `_chainState`, render all nodes, trigger badge poll
- `renderChain()` — render all nodes from `_chainState` (pure render, no fetch)
- `saveChain()` — POST `_chainState` to `/api/signal-chain` (single write, no race with per-card saves)
- `saveDenonSection()` — updates `_chainState.denon` in memory AND calls `saveChain()`. No separate Denon-only endpoint.
- `saveDspSection()` — updates `_chainState.minidsp` in memory AND calls `saveChain()`
- `addSpeakerToSlot(slotIndex, label, location, preset)` — updates `_chainState.output_slots[slotIndex]`, calls `saveChain()`
- `removeSpeakerFromSlot(slotIndex)` — clears slot in `_chainState`, calls `saveChain()`
- `pollBadges()` — called on load and on manual "Refresh" click; fetches `/api/equipment/denon/state` with 5s abort signal; updates badge only (does not re-render whole chain)

**No separate "Save Chain" button needed:** every section save writes the full `_chainState`. The "Save Chain" CTA at the bottom is an alias for `saveChain()` that provides a clear "I'm done" affordance. Per-card saves and the CTA are equivalent — last write wins, but since they all write the full state, there's no race.

## Test Plan

### New tests needed (complete list after all reviews)

**Core chain CRUD:**
- `test_signal_chain_get_empty` — GET returns Pi-only chain when no config
- `test_signal_chain_get_populated` — GET returns full chain from config
- `test_signal_chain_get_partial_slots` — config has 2 of 4 slots filled → 2 slots + 2 empty returned
- `test_signal_chain_post` — POST writes denon + minidsp + speakers, round-trips
- `test_signal_chain_post_derives_sub_outputs` — POST with 2 non-empty slots → measurement.sub_outputs=[0,1] written
- `test_signal_chain_post_empty_slots` — POST with all empty slots → measurement.sub_outputs=[]
- `test_signal_chain_post_partial_failure` — YAML write throws → 500, SQLite unchanged
- `test_signal_chain_cascade_delete` — remove DSP config → speaker slots cleared + measurement.sub_outputs=[]
- `test_signal_chain_speaker_preset` — slot with preset="pb12-nsd" and location="front-left"
- `test_signal_chain_yaml_error` — GET when config.yaml is malformed → 500 with message

**Config:**
- `test_config_output_slots_default` — DEFAULT_CONFIG has minidsp.output_slots with 4 empty entries
- `test_config_output_slots_roundtrip` — Config.load() reads output_slots correctly

**Connectivity badges:**
- `test_signal_chain_badge_timeout` — Denon unreachable → badge=FAIL within 5s, no hang
- `test_signal_chain_badge_state_denon_no_host` — no host configured → "unconfigured" state not "offline"

**Migration:**
- `test_signal_chain_migration_reads_denon_host` — existing denon.host → Denon node populated
- `test_signal_chain_migration_tombstone` — old connections.minidsp.outputs migrated to output_slots labels
- `test_signal_chain_migration_speakers_no_slot_index` — existing speakers stay in DB, not auto-mapped to slots

**Setup gate:**
- `test_signal_chain_gate_unlocks_step2` — Denon saved + ≥1 slot with speaker → gate condition true

## Migration

Users on `feat/equipment-setup` have Denon/miniDSP data in config.yaml and speakers in SQLite.
`GET /api/signal-chain` auto-populates chain from existing config on first load.

**Migration logic:**
- Read from `denon.host` (NOT `connections.minidsp`) → add Denon node if present
- Read from `connections.minidsp.outputs` → tombstone: copy labels to `minidsp.output_slots[].label`, then clear old key
- Read from `measurement.sub_outputs` → infer which output slot indices were active subs
- SQLite speaker records with no `slot_index` in data blob → **NOT auto-mapped**. Speaker records stay in DB but do not appear in output slots. User must re-add speakers via the new UI. Document clearly in empty slot UI. (No data lost — records available in SQLite.)

**Important:** `POST /api/signal-chain` MUST derive and write `measurement.sub_outputs` from non-empty output slots to keep the measurement pipeline in sync. This is the critical coupling between the new UI and the calibration loop.

## Open Questions (resolved by CEO review)

1. Delete mid-chain node → cascade delete children. No orphans.
2. Multi-chain → no, single chain only (v1).
3. Badge polling → on node expand + manual refresh button. Not continuous (reduces Denon queries).

## CEO Review Findings (autoplan 2026-03-31)

### Architecture Decision: Approach C (Extended Flat Config) over Approach A (Graph)
Subagent challenge: the topology is linear and fixed (Pi → Denon → miniDSP → 1-4 subs). A full graph model adds referential integrity complexity not justified for this use case. The visual chain builder UI is correct; the debate is the data model.

**Revised data model (Approach C):**
- Extend `minidsp` config section with `output_slots: [{index, label, location, preset}]`
- `output_slots[].location` is the single source of truth for room location (NOT equipment.data blob)
- `GET /api/signal-chain`: synthesize chain from flat config + speakers (computed read)
- `POST /api/signal-chain`: writes to `denon.*`, `minidsp.output_slots`, and speakers table
- Single source of truth: flat config. No dual-write.

### Migration Fix
Read from `denon.host` + `measurement.sub_outputs`, NOT `connections.minidsp` (old plan had wrong source).

### Error Gaps Found
- `equipment_denon_state()` has no timeout → add `asyncio.wait_for(..., timeout=5.0)` for badge
- YAML parse error on GET → explicit try/except needed
- Cascade delete: delete miniDSP config → also clear speaker records
- Pi root node: label "Calibration Host" with tooltip (Pi is not in listening signal path)

### Additional Tests Needed
- `test_signal_chain_migration_reads_denon_host` (not connections.minidsp)
- `test_signal_chain_badge_timeout` (5s timeout → offline badge, no hang)
- `test_signal_chain_cascade_delete` (delete DSP → speakers cleared)
- `test_signal_chain_partial_config` (only Denon configured, no DSP yet)

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Choose Approach C (extended flat config) over Approach A (graph model) | Taste | P5 explicit over clever | Fixed linear topology doesn't need graph; flat config is readable, queryable, no dual-write | Approach A (graph), Approach B (UI-only) |
| 2 | CEO | Cascade delete children on node removal | Mechanical | P1 completeness | Orphaned speakers are a UX bug | Orphan-on-delete |
| 3 | CEO | Badge poll on node expand + manual refresh, not continuous | Mechanical | P3 pragmatic | Reduces Denon query load, avoids race on slow networks | Continuous polling |
| 4 | CEO | Label Pi root as "Calibration Host" | Mechanical | P5 explicit | Pi is not in listening signal path; label avoids user confusion | "Pi (AVR Calibration)" |
| 5 | CEO | Include in-place speaker location editing in v1 | Mechanical | P1 completeness | Trivial JS change; prevents delete-and-re-add for location changes | Defer to v2 |
| 6 | CEO | DEFER: SQLite graph table | Taste | P3 pragmatic | Over-engineered for single user; YAML flat config sufficient | Add to this plan |
| 7 | CEO | DEFER: auto-detect topology | Taste | P3 pragmatic | Nice-to-have, not blocking core feature | Add to this plan |
| 8 | Design | Empty state: Denon card appears immediately with Discover CTA | Mechanical | P1 completeness | No spec = implementer guesses = bad first-run UX | Blank page |
| 9 | Design | "+ Add" picker: inline expand (label, location, preset + Save/Cancel) | Mechanical | P5 explicit | Unspecified picker = modal or dropdown, both worse | Modal picker |
| 10 | Design | location source of truth: output_slots[].location only, not equipment.data | Mechanical | P5 explicit | Two locations in sync is dual-write we explicitly rejected | equipment.data blob |
| 11 | Design | Setup gate: Denon saved AND ≥1 slot has speaker → Step 2 unlocks | Mechanical | P1 completeness | No gate = workflow nav is decorative | Always unlock |
| 12 | Design | Save model: explicit "Save Chain" button at bottom + per-card saves | Mechanical | P5 explicit | Auto-save needs dirty state + debounce = scope creep | Auto-save |
| 13 | Design | Loading: skeleton cards with .pending badge animation | Mechanical | P1 completeness | No loading state = layout flash on every load | No loading state |
| 14 | Design | Offline recovery: fail badge + inline Retry link + amber host highlight | Mechanical | P1 completeness | Red badge with no action = user stuck | Badge only |
| 15 | Design | Pi root: small non-card header above chain, not full card node | Mechanical | P5 explicit | Full card node misrepresents Pi as audio device | Full card |
| 16 | Design | Node expand/collapse: always expanded in v1, no accordion | Mechanical | P5 explicit | Accordion adds state machine, no benefit for 4-5 nodes | Accordion |
| 17 | Design | DEFER: keyboard navigation | Taste | P3 pragmatic | Codebase has zero keyboard nav; fixing globally is scope expansion | Fix now |
| 18 | Design | Partial state: miniDSP ghost node appears unconditionally below Denon | Mechanical | P1 completeness | No ghost node = no guidance on next step | Only show configured nodes |
| 19 | Eng | POST /api/signal-chain MUST write measurement.sub_outputs from non-empty slots | Mechanical | P1 completeness | Measurement pipeline reads sub_outputs not output_slots — without this fix, new UI is silently disconnected from calibration | Defer |
| 20 | Eng | Migration: existing SQLite speakers not auto-mapped to slots (no slot_index) | Mechanical | P5 explicit | No slot_index column; auto-mapping impossible; document clearly; user re-adds via new UI | Auto-map |
| 21 | Eng | In-memory _chainState as single JS source of truth; all saves write full state | Mechanical | P5 explicit | Eliminates per-card vs Save Chain race condition | Per-card saves only |
| 22 | Eng | Denon badge poll: use fetch AbortController with 5s timeout | Mechanical | P1 completeness | No timeout = page freezes when Denon offline | No timeout |
| 23 | Eng | DEFAULT_CONFIG must include minidsp.output_slots with 4 empty entries | Mechanical | P1 completeness | Without default, cfg.minidsp.get("output_slots") returns None for all new users | No default |
| 24 | Eng | Migration tombstone: connections.minidsp.outputs → minidsp.output_slots labels | Mechanical | P1 completeness | Old save-labels writes different YAML key; must migrate on first POST | Perpetual dual-write |
| 25 | Eng | DEFER: atomic YAML write (os.replace) | Taste | P3 pragmatic | Known limitation predates this feature; file in TODOS.md | Fix now |
| 26 | Eng | DEFER: f-string SQL in update_equipment | Taste | P3 pragmatic | Not injectable today (fixed key names); log to TODOS.md for future safety | Fix now |


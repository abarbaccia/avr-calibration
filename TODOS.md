# TODOS

## Deferred from /plan-eng-review (2026-03-19)

### TODO-1: Dry-run mode
**What:** `--dry-run` CLI flag — full measurement + AI analysis cycle runs, but no writes sent to miniDSP or Denon.
**Why:** Essential for first-run confidence before trusting the system to touch hardware. Also makes the system demonstrable without physical setup.
**Pros:** Zero-risk validation of AI judgment; useful for development and demos.
**Cons:** Small extra mode/flag to maintain in the CLI layer.
**Context:** SafetyValidator still runs in dry-run (so rejections are visible). Hardware adapter calls become no-ops that log "would write: {params}" instead of executing. Implement as an injected flag on the Adapter protocol — each adapter checks `dry_run` before writing.
**Depends on:** CLI scaffolding, hardware adapters (miniDSP + Denon).

---

### TODO-3: Content-tagged subjective feedback
**What:** Optional `content_tag` field on feedback log entries ("Fury Road chapter 3", "music: Daft Punk", "gaming: FPS"). AI analysis core groups by tag to identify content-specific patterns.
**Why:** This is the core differentiator from every other calibration tool. "Bass too heavy on action movies but not music" is a different problem than "bass too heavy" — it maps to different EQ presets.
**Pros:** Unlocks content-aware EQ profiles; maps naturally to miniDSP 2x4 HD's 4 preset slots (action / music / gaming / default).
**Cons:** Adds logging friction; content-aware profiles require multiple optimization runs (one per content type).
**Context:** Build the `content_tag` field into the feedback log schema from day one (nullable, optional). Even if unused initially, having it in the schema makes adding content-aware logic trivial later. miniDSP 2x4 HD has exactly 4 preset slots — this maps perfectly to 4 content profiles.
**Depends on:** Feedback log schema, AI analysis prompt engineering.
**Status (v0.1.1):** ✓ `content_tag` column is live in the `feedback` table; `add_feedback()` accepts and stores it. AI analysis grouping still to implement.

---

## Completed

### TODO-2: Measurement history browser
**What:** `calibrate history` CLI command — shows past sessions with date, starting FR vs. final FR, filters applied, and subjective feedback logged during that session.
**Why:** The long-term value of this tool is the accumulated room model. Without visibility, you can't tell if the system is improving over time or debug why a session diverged.
**Context:** Read-only queries against the same SQLite store the pipeline writes to. Output as a formatted table or JSON. Also `calibrate show <id>` for detail view with --csv and --json export.
**Completed:** v0.1.2 (2026-03-20)

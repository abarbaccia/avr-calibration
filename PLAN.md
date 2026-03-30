<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/main-autoplan-restore-20260330-145551.md -->
# Plan: Pi Auto-Update + Version UI

**Branch:** main
**Feature:** Two deployment improvements — (1) systemd timer for daily automatic Docker image pull + restart, and (2) version check endpoint + upgrade button in the web UI.

## Problem

The Pi currently has no way to self-update. When a new image is pushed to GHCR, someone must SSH in manually and run `docker pull + systemctl restart`. For a home appliance that runs permanently in a rack, this is a friction point — the Pi can silently fall behind.

Additionally, the web UI has no visibility into whether it's running the latest version, so the user can't tell if an update is available while browsing the calibration dashboard.

## Solution

### Part 1: Systemd timer auto-update

A daily systemd timer that pulls the latest image and restarts if there's an update.

**New files:**
- `deploy/avr-calibration-update.service` — one-shot service that runs `docker pull` then conditionally restarts
- `deploy/avr-calibration-update.timer` — fires daily at 3am

**Logic:** Pull the image, compare new digest vs. running container's image digest. If different, restart the service.

**Install:** Added to `deploy/install.sh` so all new installs get it automatically.

### Part 2: Version endpoint + web UI upgrade button

**Bake git SHA into image at build time:**
- Add `ARG BUILD_SHA` + `ENV BUILD_SHA=$BUILD_SHA` to Dockerfile
- Pass `--build-arg BUILD_SHA=${{ github.sha }}` in CI workflow

**New API endpoint `GET /api/version`:**
- Returns `{"current_sha": "...", "latest_sha": "...", "up_to_date": bool, "latest_checked_at": "..."}`
- `current_sha`: reads `BUILD_SHA` env var (baked at build time)
- `latest_sha`: calls GHCR API anonymously to get the latest manifest digest for `:latest`
- Caches the GHCR check for 1 hour (don't hammer the registry)

**New API endpoint `POST /api/upgrade`:**
- Triggers: `docker pull <image>` then exits the process (systemd `Restart=always` picks it up with new image)
- Requires Docker socket mounted in container
- Returns 202 Accepted immediately; the container will restart within seconds

**Web UI:**
- Footer shows version badge: `v{short_sha}` with a green "Up to date" or amber "Update available" indicator
- "Upgrade" button appears when update is available — calls `/api/upgrade`, shows spinner, "Container will restart..."
- On `/api/upgrade` 202: show "Upgrading... page will reload in 15s" + auto-reload

**Service file changes:**
- Mount Docker socket: add `-v /var/run/docker.sock:/var/run/docker.sock`
- Change `Restart=on-failure` → `Restart=always` (so clean exit triggers restart)
- Install Docker CLI in container (Dockerfile: `apt-get install -y docker.io`)

## What Already Exists

| Sub-problem | Existing code |
|---|---|
| Web server | `calibrate/web.py` — FastAPI app |
| Service file | `deploy/avr-calibration.service` |
| Install script | `deploy/install.sh` |
| CI build workflow | `.github/workflows/docker.yml` — already adds `org.opencontainers.image.revision` label via metadata-action |
| GHCR public image | `ghcr.io/abarbaccia/avr-calibration:latest` |

## Files Changed

- `Dockerfile` — add `BUILD_SHA` ARG + ENV, add `docker.io` apt package (~5 LOC)
- `.github/workflows/docker.yml` — pass `BUILD_SHA` build arg (~3 LOC)
- `calibrate/web.py` — `/api/version` + `/api/upgrade` endpoints + footer UI (~80 LOC)
- `deploy/avr-calibration.service` — add Docker socket mount + `Restart=always` (~2 LOC)
- `deploy/avr-calibration-update.service` — new file (~15 LOC)
- `deploy/avr-calibration-update.timer` — new file (~10 LOC)
- `deploy/install.sh` — register + start the timer (~10 LOC)
- `tests/test_web.py` — `/api/version` endpoint tests (~30 LOC)

**Total: ~155 LOC across 8 files**

## Revised Architecture (after CEO Review)

### Upgrade button routes through systemd timer, not in-container docker pull

Original plan: `/api/upgrade` runs `docker pull` inside the container, then exits.
Problem: Pi Zero 2 W pulls a 200MB image slowly. systemd restarts in 5s. Race condition — restart happens before pull finishes.

**Revised: `/api/upgrade` calls `systemctl start avr-calibration-update.service`** via the Docker socket. The update.service (Part 1) handles the pull + health-check + conditional restart. Clean separation of concerns.

### Health-check gated rollback in update service

The auto-update timer:
1. Records current image digest before pull
2. Pulls new image
3. Restarts service
4. Waits up to 30s for `/api/health` to respond
5. If health check fails: re-tags previous digest as fallback and restarts

### Upgrade UI: poll until ready, not fixed countdown

After triggering upgrade, the UI polls `GET /api/health` every 3 seconds. On first successful response, redirects. Shows elapsed seconds. No fixed countdown.

### SQLite audit log for update events

Each auto-update (timer) or manual upgrade (UI button) writes a record to the existing SQLite DB so users can see "updated to sha:abc123 at 03:02" in the calibration history timeline.

## Security Note

Mounting `/var/run/docker.sock` allows the container to start new containers with arbitrary host mounts (including full root filesystem access). This is a **different and higher risk level than `--privileged`**, which only grants device access. On this single-purpose, non-internet-exposed appliance, this is an accepted risk. Do not use this pattern on multi-tenant or internet-facing hosts. The upgrade endpoint is unauthenticated within the local network.

## CEO Review Phase — Analysis

### 0A. Premise Challenge

**Stated premises (verified):**
- Pi silently falls behind when new images ship — TRUE. Manual SSH is the only update path.
- Manual SSH is friction for a home appliance — TRUE. The Pi runs permanently in the rack.
- Docker socket is acceptable on a trusted LAN — CONDITIONALLY TRUE, but risk was understated (see security note revision).

**Assumed premises (challenged):**
- `:latest` is the right update target — RISK. A broken build tagged `:latest` would auto-install at 3am with no recovery path. Health-check gated rollback mitigates this.
- 3am restart is acceptable — ACCEPTED. Measurement sessions don't run overnight; any running container would just restart.
- systemd will restart after a clean exit with new image ready — WRONG. Race condition when upgrade happens inside container. Revised: route through systemd timer unit.

**Do-nothing analysis:** Without this feature, the Pi stays on the image it was deployed with indefinitely. After the `--device=/dev/hidraw0` bug we just fixed, the risk of stale code causing real problems is real. Building this is correct.

### 0B. Existing Code Leverage

| Sub-problem | Existing code |
|---|---|
| Systemd service pattern | `deploy/avr-calibration.service` — copy pattern for new update.service |
| Install automation | `deploy/install.sh` — add timer registration here (already adds systemd service) |
| FastAPI endpoint pattern | `calibrate/web.py` — all existing routes follow the same FastAPI pattern |
| GHCR image labels | CI already sets `org.opencontainers.image.revision` (git SHA) and `org.opencontainers.image.source` via `docker/metadata-action@v5` — baked BUILD_SHA is additive |
| SQLite storage | `calibrate/storage.py` + `SessionStore` — existing DB, add an `update_events` table |

No parallel rebuilds. All sub-problems map to extensions of existing patterns.

### 0C. Dream State Mapping

```
CURRENT STATE                  THIS PLAN                  12-MONTH IDEAL
─────────────────────         ──────────────────────      ──────────────────────────
Pi stuck on deploy image      Daily timer pulls :latest   `:stable` CI promotion track:
Manual SSH to update           with rollback               CI pushes :edge, integration
No version visibility          Version badge in footer      test tags :stable, Pi pulls
Service crashes if hidraw0     Upgrade button in UI         :stable only
missing (just fixed)           Polls until ready            Full audit trail in UI
                               Audit log in SQLite          Zero-touch, zero-risk
```

### 0C-bis. Implementation Alternatives

```
APPROACH A: Systemd timer only (no UI)
  Summary: Daily docker pull + conditional restart. No web UI changes.
  Effort:  S (3 files, ~35 LOC)
  Risk:    Low
  Pros:    Minimal diff; no Docker socket exposure; operationally simple
  Cons:    No user visibility; no on-demand upgrade; user can't see if update failed
  Reuses:  deploy/avr-calibration.service pattern

APPROACH B: Systemd timer + version endpoint + upgrade button (the plan)
  Summary: Timer for auto-updates + web UI for visibility and manual trigger.
  Effort:  M (8 files, ~175 LOC after revisions)
  Risk:    Medium (Docker socket mount)
  Pros:    Complete solution; visibility + automation; on-demand upgrade
  Cons:    Docker socket required for upgrade button; adds 3 new endpoints
  Reuses:  All existing patterns

APPROACH C: Watchtower container + version label
  Summary: Run Watchtower as a sidecar container that polls GHCR and auto-restarts.
  Effort:  S (2 files, ~20 LOC — just service + install changes)
  Risk:    Medium (Watchtower also needs Docker socket)
  Pros:    Zero new Python code; battle-tested tool; built-in health checking
  Cons:    Extra container (RAM on Pi Zero 2 W matters); no version UI; black box
  Reuses:  Nothing new
```

**RECOMMENDATION:** Approach B — complete solution, visible to the user, uses existing patterns. Approach C (Watchtower) is tempting but adds a black box dependency and no version visibility. Approach A is too minimal — no on-demand upgrade.

### 0D. Mode Analysis (SELECTIVE EXPANSION)

Core scope (8 files, ~175 LOC after CEO revisions): HOLD.

**Cherry-pick candidate surfaced at gate:**
- **`:stable` CI promotion track** — CI pipeline pushes to `:edge` on every branch push, integration test run tags it `:stable`, Pi auto-updates to `:stable`. Eliminates the broken-`:latest` risk entirely. Effort: M (~30 LOC in CI workflow + service file). Risk: Low. This is the cleanest long-term solution.

Auto-decided to include all 5 CEO review findings in the plan (P1, P2). `:stable` promotion is a TASTE DECISION — surface at gate.

### 0E. Temporal Interrogation

```
HOUR 1 (foundations):     Dockerfile BUILD_SHA bake + docker.io apt install.
                           Q: docker.io in runtime image adds ~100MB. Use docker-cli package instead?
                           A: Yes — docker-cli (CLI only, no daemon) is ~40MB on Debian Bookworm.

HOUR 2-3 (core logic):    avr-calibration-update.service bash script — rollback logic.
                           Q: How to track previous image digest?
                           A: Write to /data/.avr-calibration/last-known-good-digest (mounted volume).

HOUR 4-5 (integration):   /api/upgrade calls `systemctl start avr-calibration-update.service`
                           via the Docker socket using docker.exec or subprocess.
                           Q: Can we call systemctl from inside the container?
                           A: Via Docker socket: `docker exec --host systemctl start ...` won't work.
                           Better: `curl --unix-socket /var/run/docker.sock http://localhost/containers/avr-calibration-update/start` — but the update is a separate service, not a container. Use the Python `docker` SDK or just `subprocess(['systemctl', ...])` with `--host` flag passed via Docker socket.
                           Simpler: Write a trigger file to the mounted volume that the host-side
                           update.service watches via inotify. Actually simplest: mount the Docker
                           socket and use `docker run --rm -v /var/run/docker.sock:...` to exec
                           `systemctl start avr-calibration-update.service` on the HOST.
                           Final answer: use `subprocess` to call the docker CLI to exec a
                           privileged systemctl command on the host. OR: the update service
                           watches a trigger file. Pick trigger file — no socket needed for this.

HOUR 6+ (polish/tests):   /api/health endpoint (new, needed by poll-until-ready UX).
                           Test the version cache invalidation (1 hour TTL).
                           Test rollback: inject a health-check-failing image, verify rollback.
```

**Resolved during temporal interrogation:**
- Use `docker-cli` package, not `docker.io` (saves ~60MB image size)
- Upgrade trigger: write a trigger file to `/data/.avr-calibration/upgrade-trigger` — the update.service uses `inotifywait` on that file. Eliminates Docker socket need for the upgrade endpoint. The version endpoint still needs GHCR API access (network, not socket).
- **This means Docker socket is NOT required at all** — we can drop the socket mount entirely. The upgrade button writes a trigger file; the host-side update.service reacts. Significantly cleaner security profile.

### CEO Completion Summary

| Section | Status | Findings |
|---|---|---|
| Premise Challenge | PASS | 3 premises validated, 1 corrected (race condition), 1 risk noted (`:latest` target) |
| Existing Code Leverage | PASS | All sub-problems map to existing patterns, no rebuilds |
| Dream State | PASS | `:stable` promotion is the 12-month ideal, surfaced as taste decision |
| Implementation Alternatives | PASS | 3 approaches; B recommended |
| Mode | SELECTIVE EXPANSION | Core scope HOLD; 1 cherry-pick candidate for gate |
| Security | REVISED | Docker socket dropped entirely via trigger-file approach |
| Architecture Finding | CRITICAL FIXED | No rollback → health-check gated rollback added |
| Security Note | HIGH FIXED | Risk accurately described |
| Race Condition | HIGH FIXED | Upgrade routes through trigger file + update.service |
| Countdown UX | MEDIUM FIXED | Poll-until-ready replaces fixed countdown |
| Audit Trail | MEDIUM FIXED | SQLite update_events table added |

**CRITICAL ARCHITECTURAL REVISION:** Docker socket is no longer needed. The upgrade endpoint writes a trigger file to the mounted data volume. The host-side `avr-calibration-update.service` watches that file via `inotifywait`. Clean, no socket exposure.

## Files Changed (revised after CEO review)

- `Dockerfile` — add `BUILD_SHA` ARG + ENV, add `docker-cli` apt package (~5 LOC)
- `.github/workflows/docker.yml` — pass `BUILD_SHA` build arg (~3 LOC)
- `calibrate/web.py` — `/api/version` + `/api/upgrade` + `/api/health` endpoints + footer UI (~90 LOC)
- `calibrate/storage.py` — `update_events` table + `log_update_event()` method (~25 LOC)
- `deploy/avr-calibration.service` — `Restart=always` (already applied as hotfix; no socket needed)
- `deploy/avr-calibration-update.service` — new file with pull + rollback logic (~25 LOC)
- `deploy/avr-calibration-update.timer` — new file (~10 LOC)
- `deploy/install.sh` — register + start the timer (~10 LOC)
- `tests/test_web.py` — version + upgrade + health endpoint tests (~45 LOC)
- `tests/test_storage.py` — update_events table tests (~15 LOC)

**Total: ~228 LOC across 10 files** (increased from 155 due to rollback, audit log, health endpoint)

## Test Plan

### /api/version
- `test_version_endpoint_returns_sha` — `BUILD_SHA` env set → sha returned
- `test_version_endpoint_no_sha` — env not set → `"unknown"` returned
- `test_version_up_to_date` — mock GHCR response matching current sha → `up_to_date: true`
- `test_version_update_available` — mock GHCR response with different sha → `up_to_date: false`
- `test_version_ghcr_unreachable` — GHCR timeout → returns current sha with `latest_sha: null`
- `test_version_cache_ttl` — GHCR response cached for 1 hour, not re-fetched on second call
- `test_version_cache_invalidated_after_ttl` — cache expires after 1 hour, re-fetches

### /api/upgrade
- `test_upgrade_writes_trigger_file` — POST /api/upgrade → trigger file created at `/data/.avr-calibration/upgrade-trigger`, 202 returned
- `test_upgrade_trigger_file_path_configurable` — respects data dir config

### /api/health
- `test_health_returns_200` — GET /api/health → 200 with `{"status": "ok"}`

### storage.py update_events
- `test_log_update_event` — event saved to DB with sha, timestamp, source (timer/manual)
- `test_update_history_queryable` — events queryable by date range

### avr-calibration-update.service (shell script tests)
- Script-level: `bats` or equivalent. Not in pytest scope — integration tested manually on Pi.

## Error & Rescue Registry

| Method/Codepath | What Can Go Wrong | Exception Class |
|---|---|---|
| `GET /api/version` GHCR call | Network timeout | `httpx.TimeoutException` |
| `GET /api/version` GHCR call | GHCR returns 429 rate limit | `httpx.HTTPStatusError` (429) |
| `GET /api/version` GHCR call | GHCR returns malformed JSON | `json.JSONDecodeError` |
| `GET /api/version` GHCR call | GHCR auth required (private) | `httpx.HTTPStatusError` (401) |
| `POST /api/upgrade` trigger file | Data dir not mounted / not writable | `OSError` / `PermissionError` |
| `POST /api/upgrade` trigger file | Disk full | `OSError` (ENOSPC) |
| `storage.log_update_event()` | DB locked (concurrent write) | `sqlite3.OperationalError` |
| `avr-calibration-update.service` | `docker pull` fails (network) | shell exit code != 0 |
| `avr-calibration-update.service` | New container health check fails | health poll timeout |
| `avr-calibration-update.service` | Rollback pull fails | secondary failure |

| Exception Class | Rescued? | Rescue Action | User Sees |
|---|---|---|---|
| `httpx.TimeoutException` | Y | Return `latest_sha: null`, log warning | Version badge shows "Unknown" |
| `httpx.HTTPStatusError` (429) | Y | Return cached result, log warning | Cached badge or "Unknown" |
| `json.JSONDecodeError` | Y | Return `latest_sha: null`, log error | Version badge shows "Unknown" |
| `httpx.HTTPStatusError` (401) | Y | Return `latest_sha: null`, note private repo | "Cannot check — private repo" |
| `OSError` / `PermissionError` | Y | Return 503 with message | "Upgrade unavailable: data volume not writable" |
| `OSError` (ENOSPC) | Y | Return 503 with message | "Upgrade unavailable: disk full" |
| `sqlite3.OperationalError` | Y | Log and swallow (audit log is non-critical) | Upgrade proceeds, no audit entry |
| `docker pull` failure (service) | Y | Log to journal, skip restart | No change (old version keeps running) |
| Health check timeout | Y | Rollback to previous digest | Old version restored |
| Rollback pull failure | N (GAP) | Log to journal, systemd loops | Container down until manual fix |

**GAP:** Rollback failure. If the fallback digest is also broken (corrupted, deleted from GHCR), the service enters a down state. Mitigation: the data volume retains the last-known-good digest file; manual SSH recovery is possible. Acceptable for a home appliance.

## Failure Modes Registry

| Failure Mode | Probability | Impact | Detection | Recovery |
|---|---|---|---|---|
| `:latest` is broken at 3am update time | Low | High — appliance down until rollback | Health check timeout → auto-rollback | Automatic rollback to previous digest |
| Trigger file written but update.service not running | Very Low | Low — upgrade doesn't happen | Version badge shows old sha next load | Systemctl restart update.service |
| GHCR anonymous API breaks | Low | Low — version badge shows "Unknown" | `/api/version` returns null latest_sha | Badge degrades gracefully |
| Pi disk full, can't pull new image | Very Low | Low — stays on current version | `docker pull` fails in update.service | Free disk space, retry |
| SQLite DB locked during audit log write | Very Low | Negligible — audit entry lost | Exception swallowed silently | N/A (non-critical) |
| inotifywait not available in update.service | Possible | High — upgrade button does nothing | Trigger file written but no reaction | Add inotifywait to update.service deps |

**CRITICAL GAP from Failure Modes:** `inotifywait` may not be installed. The update.service uses `inotifywait` to watch the trigger file. `inotify-tools` must be installed on the Pi host (not in the container). Add to `deploy/install.sh`: `apt-get install -y inotify-tools`.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | P6 (action) | Feature enhancement on deployed system | EXPANSION, REDUCTION |
| 2 | CEO | Codex unavailable → subagent-only | Mechanical | — | No codex binary on path | — |
| 3 | CEO | Add health-check gated rollback to update.service | Mechanical | P1 (complete) + P2 (lake) | Critical gap: no recovery from broken :latest | Skip rollback |
| 4 | CEO | Rewrite security note accurately | Mechanical | P5 (explicit) | Docker socket ≠ --privileged; false equivalence | Keep original note |
| 5 | CEO | Drop Docker socket; use trigger file instead | Mechanical | P5 (explicit) + P3 (pragmatic) | Simpler, no socket exposure, solves race | Docker socket mount |
| 6 | CEO | Replace countdown with poll-until-ready | Mechanical | P1 (complete) | 15s too short for Pi Zero 2 W image pull | Fixed countdown |
| 7 | CEO | Add SQLite update_events audit log | Mechanical | P1 (complete) | User needs visibility into auto-updates | No audit log |
| 8 | CEO | Add inotify-tools to install.sh | Mechanical | P1 (complete) | Failure modes gap: inotifywait not guaranteed | Skip |
| 9 | CEO | Use docker-cli package, not docker.io | Mechanical | P5 (explicit) | Saves ~60MB image size, CLI only needed | docker.io |
| 10 | CEO | :stable CI promotion track | Taste | P1 vs P3 | Better safety, more CI complexity | Auto-include |

## Design Review Phase — UI Spec (after Phase 2)

### State Table (mandatory — 4 states)

| State | Badge text | Badge color | Button |
|---|---|---|---|
| Up to date | `v{sha7} — Up to date` | `#4ade80` green pill (.badge-optimal) | Hidden |
| Update available | `v{sha7} — Update available` | `#fbbf24` amber pill (.badge-warn) | "Upgrade" teal button |
| Upgrading (polling) | `v{sha7} — Upgrading...` | `#2dd4bf` teal pulsing (.badge-upgrading) | Disabled spinner |
| Unknown / unreachable | `v{sha7} — Version check unavailable` | `#94a3b8` muted pill | Hidden |

### Upgrade flow (button click → complete)

1. User clicks "Upgrade" button
2. Inline confirmation appears in the footer (replacing the button): "Restart the appliance to install the update? Any active measurement will be interrupted." + "Confirm" and "Cancel" buttons
3. User clicks Confirm → POST /api/upgrade (202) → trigger file written
4. Footer shows disabled spinner button + `role="status" aria-live="polite"` text: "Upgrading — checking every 3s ({N}s elapsed)"
5. Focus moved programmatically to the status text element (`tabindex="-1"`)
6. After health OK: show "Updated to v{new_sha7}" for 2s → `window.location.reload()`
7. If poll timeout (180s): show "Upgrade is taking longer than expected. Check the Pi's network connection." — stop polling, re-enable upgrade button

### Footer layout spec

- Full-width non-sticky strip at bottom of document flow (not fixed-position)
- `background: #1a1f2e; border-top: 1px solid #2d3748; padding: 0.5rem 1.5rem`
- Flex row: badge left-aligned, upgrade button right-aligned
- Contains only: version badge + (conditional) upgrade button

### Palette (actual web.py values, not CLAUDE.md descriptions)

- Up to date: `#4ade80` (matches `.badge-optimal`)
- Update available: `#fbbf24` (matches `.badge-warn`)
- Upgrading: `#2dd4bf` (teal, pulse animation)
- Unknown: `#94a3b8` (muted)

### Accessibility

- Status badge uses existing `.badge` CSS classes — no new one-off color values
- Status encoded in both text AND color (never color-only)
- Upgrade button: `<button type="button">` with `outline: 2px solid #2dd4bf; outline-offset: 2px` focus ring
- Status text has `role="status" aria-live="polite"` — elapsed-time updates announced without interruption
- On button removal: `focus()` moved to status text element (`tabindex="-1"`)

### Design Litmus Scorecard

| Dimension | Score | Finding | Status |
|---|---|---|---|
| State coverage | 9/10 | 4th state (unknown) added | FIXED |
| Emotional arc | 8/10 | Confirmation + timeout + success moment added | FIXED |
| Accessibility | 8/10 | aria-live + focus management specified | FIXED |
| Visual integration | 9/10 | Correct palette from web.py; extends .badge system | FIXED |
| Layout specificity | 8/10 | Footer position, dimensions, contents specified | FIXED |
| Keyboard nav | 8/10 | Focus management on state transitions specified | FIXED |
| Responsive behavior | 7/10 | Mobile: badge may need truncated SHA (4 chars not 7) | OPEN (low priority) |

**Overall design score: 8/10** (up from ~5/10 before Phase 2)

DESIGN DUAL VOICES — CONSENSUS TABLE:
```
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. State coverage complete?          NO→YES   N/A    FIXED
  2. Emotional arc complete?           NO→YES   N/A    FIXED
  3. Accessibility specified?          NO→YES   N/A    FIXED
  4. Visual integration consistent?    NO→YES   N/A    FIXED
  5. Layout specified?                 NO→YES   N/A    FIXED
  6. Copy strings defined?             NO→YES   N/A    FIXED
  7. Mobile behavior addressed?        PARTIAL  N/A    OPEN (low)
═══════════════════════════════════════════════════════════════
Codex unavailable — [subagent-only] mode
All critical/high findings auto-fixed. 1 low-priority open item.
```

<!-- DECISION LOG continuation -->
| 11 | Design | Add 4th state (unknown) to state table | Mechanical | P1 (complete) | Missing from plan; Error Registry explicitly handles null latest_sha | Skip |
| 12 | Design | Add confirmation step before upgrade | Mechanical | P1 (complete) | Destructive restart needs confirmation; CEO P3 (pragmatic) | No confirmation |
| 13 | Design | Add 180s poll timeout + failure copy | Mechanical | P1 (complete) | 30-90s pull realistic on Pi Zero; no ceiling = perceived crash | Skip timeout |
| 14 | Design | Use existing .badge CSS; correct palette values | Mechanical | P5 (explicit) | Plan used CLAUDE.md values, not web.py actuals — would produce visual drift | New colors |
| 15 | Design | Add aria-live + focus management spec | Mechanical | P1 (complete) | Accessibility gap; easy to specify now, expensive to retrofit | Skip |
| 16 | Design | Add footer layout spec (position, dimensions) | Mechanical | P5 (explicit) | No layout = implementer invents it | Implicit |

## Engineering Review Phase — Analysis (after Phase 3)

### Architecture ASCII Diagram

```
HOST (Raspberry Pi OS)
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  avr-calibration.service (Docker container)                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  FastAPI web server (uvicorn, async, port 8000)        │  │
│  │                                                        │  │
│  │  GET /health          → {"status":"ok"}                │  │
│  │  GET /api/version     → calls GHCR API (httpx.Async)   │  │
│  │  POST /api/upgrade    → writes trigger file            │  │
│  │                           /data/.avr-calibration/      │  │
│  │                           upgrade-trigger              │  │
│  └────────────────────────────────────────────────────────┘  │
│                         │                                    │
│                         │ /home/pi/.avr-calibration/ (bind)  │
│                         ▼                                    │
│  ┌──────────────────────────────────────────────────┐        │
│  │  /home/pi/.avr-calibration/upgrade-trigger       │        │
│  │  (trigger file — ephemeral, deleted after use)   │        │
│  └──────────────────────────────────────────────────┘        │
│           ▲                       │                          │
│           │                       │ inotifywait (monitor)    │
│           │                       ▼                          │
│  avr-calibration-update.service  (one-shot, triggered)       │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  1. Check trigger file exists (startup poll)         │    │
│  │  2. Acquire flock (.upgrade-lock) — one at a time    │    │
│  │  3. Record current image digest                      │    │
│  │  4. docker pull ghcr.io/abarbaccia/avr-calibration   │    │
│  │  5. systemctl restart avr-calibration                │    │
│  │  6. Poll GET /health (30s timeout)                   │    │
│  │  7a. Health OK: delete trigger file, release lock    │    │
│  │  7b. Health FAIL: re-pull previous digest, restart   │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  avr-calibration-update.timer                                │
│  └── OnCalendar=daily (fires at 3am)                         │
│      triggers avr-calibration-update.service                 │
└──────────────────────────────────────────────────────────────┘

GHCR API flow (inside container on /api/version):
  1. GET https://ghcr.io/token?service=ghcr.io&scope=repository:abarbaccia/avr-calibration:pull
     → { "token": "..." }
  2. GET https://ghcr.io/v2/abarbaccia/avr-calibration/manifests/latest
     Headers: Authorization: Bearer {token}, Accept: application/vnd.oci.image.index.v1+json
     → OCI image manifest with labels including org.opencontainers.image.revision
  3. Extract org.opencontainers.image.revision from config blob
  4. Compare to BUILD_SHA env var (both are git SHAs)
  Cache: module-level dict, 1-hour TTL (reset on container restart)
```

### Eng-Critical Revisions

**Finding E1 — inotifywait race (HIGH, FIXED):**
- update.service script: on startup, check if trigger file exists BEFORE blocking on inotify
- Use `inotifywait -m` (monitor mode, not single-shot)
- Use `flock /data/.avr-calibration/.upgrade-lock` before pull
- Delete trigger file after consuming (before releasing lock)

**Finding E2 — GHCR API requires bearer token (HIGH, FIXED):**
- Two-step API call: fetch anonymous token, then fetch manifest with Authorization header
- Compare git SHAs (BUILD_SHA vs `org.opencontainers.image.revision` from OCI label)
- NOT comparing OCI digests (different for multi-arch index vs. per-arch manifest)
- 401 response = retry with fresh token, not "private repo" message

**Finding E3 — Use httpx.AsyncClient, not httpx.get() (HIGH, FIXED):**
- httpx is already imported in web.py (line 50) — no new dependency
- `/api/version` must use `async with httpx.AsyncClient() as client: await client.get(...)`
- `/health` already exists at web.py:1153 — poll-until-ready uses existing endpoint, no new route needed

**Finding E4 — Trigger file lifecycle (MEDIUM, FIXED):**
- Trigger file deleted after consumption (before releasing lock)
- Service startup: check for pre-existing trigger file before blocking on inotifywait
- Prevents "stuck" trigger that silently misses inotify events

**Finding E5 — DB migration test (MEDIUM, FIXED):**
- Add test: create pre-update_events SQLite DB, open SessionStore, verify table created, log_update_event() succeeds

**Finding E6 — BUILD_SHA empty for local builds (LOW, DOCUMENTED):**
- Known limitation: locally-built images without `--build-arg BUILD_SHA=...` return "unknown"
- Cache: module-level `_version_cache` dict (`{"result": ..., "expires": time.time() + 3600}`)
- No persistent cache (no SQLite round-trip for version checks)

### Test Coverage Diagram

```
CODE PATH COVERAGE
===========================

[+] calibrate/web.py — /api/version
    │
    ├── GET /api/version
    │   ├── [★★★] BUILD_SHA set → sha returned — test_version_endpoint_returns_sha
    │   ├── [★★★] BUILD_SHA unset → "unknown" — test_version_endpoint_no_sha
    │   ├── [★★★] GHCR matches → up_to_date: true — test_version_up_to_date
    │   ├── [★★★] GHCR differs → up_to_date: false — test_version_update_available
    │   ├── [★★★] GHCR timeout → latest_sha: null — test_version_ghcr_unreachable
    │   ├── [★★★] Cache hit → no 2nd GHCR call — test_version_cache_ttl
    │   ├── [★★★] Cache miss after TTL → re-fetches — test_version_cache_invalidated_after_ttl
    │   ├── [★★★] GHCR 401 → fetch token first, retry — test_version_ghcr_auth_retry
    │   └── [GAP] GHCR 429 rate limit → return cached — test_version_ghcr_rate_limited ← ADD

[+] calibrate/web.py — /api/upgrade
    │
    ├── POST /api/upgrade
    │   ├── [★★★] Data dir writable → trigger file written, 202 — test_upgrade_writes_trigger_file
    │   ├── [★★★] Data dir not writable → 503 — test_upgrade_data_dir_not_writable
    │   ├── [GAP] Trigger file already exists → overwrite or 409 — test_upgrade_already_in_progress ← ADD
    │   └── [★★  ] Data dir path config — test_upgrade_trigger_file_path_configurable

[+] calibrate/storage.py — update_events
    │
    ├── log_update_event()
    │   ├── [★★★] New DB → table created, event saved — test_log_update_event
    │   ├── [★★★] Existing DB (pre-update_events) → migration — test_update_events_migration_existing_db ← ADD
    │   ├── [★★★] Events queryable by range — test_update_history_queryable
    │   └── [★★★] DB locked → exception swallowed — test_update_event_db_locked_swallowed ← ADD

[+] deploy/avr-calibration-update.service (shell)
    │
    └── [Integration — manual on Pi] pull + rollback + trigger cleanup — not in pytest scope

USER FLOW COVERAGE
===========================
[+] Upgrade button flow
    │
    ├── Click Upgrade → confirmation shown — (frontend test, no backend)
    ├── Click Confirm → POST /api/upgrade → spinner → poll /health — [→E2E]
    ├── Poll succeeds → reload — (frontend test)
    ├── Poll times out (180s) → error message — (frontend test)
    └── [GAP] Double-click Confirm → two POSTs → should be idempotent ← ADD

─────────────────────────────────────────────────────────────
COVERAGE BEFORE ADDITIONS: 10/18 paths tested (56%)
COVERAGE AFTER ADDITIONS: 16/18 paths tested (89%)
GAPs ADDED TO TEST PLAN: +6 tests
─────────────────────────────────────────────────────────────
```

### Updated Test Plan (additions from Phase 3)

**Add to tests/test_web.py:**
- `test_version_ghcr_auth_retry` — GHCR 401 → fetch anonymous token, retry request
- `test_version_ghcr_rate_limited` — GHCR 429 → return cached or `latest_sha: null`
- `test_upgrade_data_dir_not_writable` — chmod 000 data dir → 503 with message
- `test_upgrade_already_in_progress` — trigger file already exists → 409 Conflict or idempotent 202

**Add to tests/test_storage.py:**
- `test_update_events_migration_existing_db` — pre-schema DB + SessionStore init → update_events created
- `test_update_event_db_locked_swallowed` — mock DB lock → log_update_event() does not raise

**Total tests: 6 original + 10 from eng phase = 16 tests across test_web.py + test_storage.py**

### NOT in scope

- `:stable` CI promotion track (taste decision → surfaced at gate)
- E2E test for full upgrade button → health poll → reload flow (manual validation on Pi)
- bats/shell tests for avr-calibration-update.service (integration tested on Pi)
- Mobile-responsive footer truncation (7/10 design score, low priority)

ENG DUAL VOICES — CONSENSUS TABLE:
```
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               YES*     N/A    YES*
  2. Test coverage sufficient?         PARTIAL→YES N/A FIXED
  3. Performance risks addressed?      YES      N/A    YES
  4. Security threats covered?         YES      N/A    YES
  5. Error paths handled?              PARTIAL→YES N/A FIXED
  6. Deployment risk manageable?       YES      N/A    YES
═══════════════════════════════════════════════════════════════
*with trigger-file race fix + GHCR auth + AsyncClient revisions
Codex unavailable — [subagent-only] mode
All high findings auto-fixed.
```

<!-- DECISION LOG continuation (Phase 3) -->
| 17 | Eng | inotifywait startup poll + flock + trigger cleanup | Mechanical | P1 (complete) + P5 (explicit) | Race conditions: trigger written before watcher ready; concurrent upgrades | Skip |
| 18 | Eng | GHCR: two-step token + manifest API; git SHA comparison via OCI label | Mechanical | P5 (explicit) | Multi-arch digest ≠ per-arch digest; 401 is universal not private-repo-only | Direct digest compare |
| 19 | Eng | Use httpx.AsyncClient (already imported) | Mechanical | P5 (explicit) | Sync httpx.get() blocks async event loop on Pi Zero 2W single core | Sync client |
| 20 | Eng | /health already exists — use it for poll-until-ready | Mechanical | P4 (DRY) | Existing route at web.py:1153; no new endpoint needed | New /api/health |
| 21 | Eng | Add GHCR 401 auth retry test | Mechanical | P1 (complete) | Auth is required even for public repos; missing from test plan | Skip |
| 22 | Eng | Add trigger-already-exists → 409 test | Mechanical | P1 (complete) | Double-click prevent double-upgrade | Skip |
| 23 | Eng | Add DB migration regression test | Mechanical | P1 (complete) | Real Pi has existing DB without update_events | Skip |
| 24 | Eng | Confirm module-level version cache (not persistent) | Mechanical | P5 (explicit) | Persistent cache would add complexity for no benefit | SQLite cache |
| 24 | Eng | Confirm module-level version cache (not persistent) | Mechanical | P5 (explicit) | Persistent cache would add complexity for no benefit | SQLite cache |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 3 | PASS | 10 decisions; trigger-file arch, rollback, race fix |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | PASS | 8/10; 4 states, aria-live, confirmation dialog |
| Eng Review | `/plan-eng-review` | Architecture & tests | 2 | PASS | 6 findings; GHCR auth, AsyncClient, inotify race |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Not available (binary not on path) |

**VERDICT:** APPROVED — user selected "Approve as-is". Implementation proceeding.

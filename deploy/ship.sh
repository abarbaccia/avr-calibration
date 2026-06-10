#!/usr/bin/env bash
# deploy/ship.sh — one-shot full pipeline: commit → push → merge → CI build → pull on Pi
#
# This is the SLOW lane (immutable image in git + registry). For fast iteration
# that skips git and CI, use ./deploy/hotfix.sh instead.
#
# Usage:
#   ./deploy/ship.sh "fix(trinnov): tighten regularization"   # branch, PR, merge, build, pull
#   ./deploy/ship.sh                                           # no changes: just re-pull :latest on the Pi
#   PI_HOST=192.168.1.50 ./deploy/ship.sh "msg"                # override Pi address
#   NO_PR=1 ./deploy/ship.sh "msg"                             # commit straight to main (skip the PR dance)
#
# Requires: gh (authenticated), ssh access to the Pi.

set -euo pipefail

PI_HOST="${PI_HOST:-192.168.1.117}"
PI_USER="${PI_USER:-pi}"
SERVICE="avr-calibration"
IMAGE="${IMAGE:-ghcr.io/abarbaccia/avr-calibration:latest}"
BUILD_WORKFLOW="${BUILD_WORKFLOW:-Build and push Docker image}"
MSG="${1:-}"
SSH="ssh -o ConnectTimeout=10 ${PI_USER}@${PI_HOST}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

# ── 1. Commit any changes ──────────────────────────────────────────────────────
if ! git diff --quiet || ! git diff --cached --quiet; then
    [ -n "$MSG" ] || { echo "ERROR: you have changes but gave no commit message."; echo "  Usage: $0 \"commit message\""; exit 1; }
    say "Committing changes"
    git add -A
    git status --short
    git commit -q -m "$MSG"
else
    say "No local changes — will (re)deploy current origin/main"
fi

BRANCH="$(git branch --show-current)"

# ── 2. Push + land on main ─────────────────────────────────────────────────────
if [ "$BRANCH" = "main" ] || [ "${NO_PR:-0}" = "1" ]; then
    if [ "$BRANCH" != "main" ]; then
        say "NO_PR set — fast-forwarding $BRANCH onto main"
        git checkout main && git merge --ff-only "$BRANCH"
    fi
    say "Pushing main"
    git push origin main
else
    say "Pushing $BRANCH and opening a PR"
    git push -u origin "$BRANCH"
    gh pr create --fill --base main >/dev/null 2>&1 || true
    say "Squash-merging the PR"
    gh pr merge "$BRANCH" --squash --delete-branch
fi

# ── 3. Wait for the CI image build triggered by the merge ───────────────────────
say "Waiting for CI build to register"
RUN_ID=""
for _ in $(seq 1 20); do
    RUN_ID="$(gh run list --workflow "$BUILD_WORKFLOW" --branch main --event push \
              --limit 1 --json databaseId,status -q '.[0].databaseId' 2>/dev/null || true)"
    [ -n "$RUN_ID" ] && break
    sleep 3
done
[ -n "$RUN_ID" ] || { echo "ERROR: could not find a '$BUILD_WORKFLOW' run on main."; exit 1; }

say "Watching build run $RUN_ID (arm64, ~1-3 min)"
gh run watch "$RUN_ID" --exit-status --interval 15

# ── 4. Pull on the Pi + restart + health check ─────────────────────────────────
say "Pulling new image on $PI_HOST and restarting $SERVICE"
$SSH "sudo docker pull '$IMAGE' | tail -2 \
   && sudo systemctl restart '$SERVICE' \
   && sleep 4 \
   && systemctl is-active '$SERVICE' \
   && sudo docker images '$IMAGE' --format 'live image: {{.ID}} ({{.CreatedSince}})'"

say "Shipped ✓"

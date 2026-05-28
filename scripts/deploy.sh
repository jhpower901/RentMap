#!/usr/bin/env bash
# Production deploy: git pull + rebuild + restart, preserving the auto-
# generated web bundles that are no longer tracked.
#
# web/dabang.html, daangn.html, zigbang.html, naver.html, index.html, and
# all data_*_<slug>.js files are produced by ``rentmap.py gen-web`` after
# every crawl. Since 69278e8 they're .gitignored, so ``git pull`` deletes
# whatever was checked in before. Without preservation the platform pages
# 404 until the next scheduled crawl regenerates them (up to ~6h).
#
# This script backs the live files up to /tmp before the pull and restores
# them after, so the site keeps serving the previous crawl's data through
# the upgrade. The next regularly-scheduled crawl overwrites them with
# fresh DB-sourced output.
#
# Usage:
#   cd /opt/docker/RentMap && bash scripts/deploy.sh [--no-build]
#
# Flags:
#   --no-build   Skip ``docker compose build`` and just restart. Use when
#                only scripts/ or docs/ changed (the scripts dir is volume-
#                mounted so a restart picks up Python changes without
#                rebuilding the image).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DO_BUILD=1
for arg in "$@"; do
    case "$arg" in
        --no-build) DO_BUILD=0 ;;
        -h|--help)
            sed -n '2,/^$/{/^#/p;}' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "deploy: unknown flag: $arg" >&2
            echo "usage: $0 [--no-build]" >&2
            exit 2
            ;;
    esac
done

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# Unique backup dir so concurrent runs (shouldn't happen, but defensive)
# don't trample each other.
BACKUP_DIR="$(mktemp -d -t rentmap-web-backup.XXXXXX)"

# Restore on any exit path so a failed pull doesn't leave production
# without its web bundles. The restore is idempotent — copying back
# what we just copied out is a no-op if the pull didn't touch them.
restore_and_cleanup() {
    local rc=$?
    if [ -d "$BACKUP_DIR" ]; then
        # Use cp -n (no-clobber) so a fresh build that managed to regen
        # files (e.g. via gen-web in the new container) isn't overwritten
        # by the backup. The backup only fills in the gaps left by pull.
        if compgen -G "$BACKUP_DIR/*" >/dev/null; then
            log "restoring auto-generated files from $BACKUP_DIR"
            cp -n "$BACKUP_DIR"/* web/ 2>/dev/null || true
        fi
        rm -rf "$BACKUP_DIR"
    fi
    if [ "$rc" -ne 0 ]; then
        log "deploy FAILED with exit $rc"
    else
        log "deploy OK"
    fi
    exit "$rc"
}
trap restore_and_cleanup EXIT

# -------- 1. Backup --------
log "backing up auto-generated web files to $BACKUP_DIR"
# Globs may not match anything on a fresh clone; failglob would abort.
# `|| true` keeps us going so the first-ever deploy doesn't break.
cp web/dabang.html web/daangn.html web/zigbang.html \
   web/naver.html  web/index.html  "$BACKUP_DIR"/ 2>/dev/null || true
cp web/data_*.js "$BACKUP_DIR"/ 2>/dev/null || true
BACKUP_COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -type f | wc -l)
log "backed up $BACKUP_COUNT file(s)"

# -------- 2. Pull --------
log "fetching origin"
git fetch origin

LOCAL_REF="$(git rev-parse HEAD)"
REMOTE_REF="$(git rev-parse '@{u}')"
if [ "$LOCAL_REF" = "$REMOTE_REF" ]; then
    log "already up to date with origin (HEAD=$LOCAL_REF)"
else
    log "pulling: $LOCAL_REF -> $REMOTE_REF"
    git log --oneline "$LOCAL_REF..$REMOTE_REF"

    # Pre-pull guard: if the incoming change ADDS a file we already
    # have on disk untracked, git refuses ("would be overwritten by
    # merge") even when the local copy is identical. Move conflicting
    # files into a dedicated backup so the pull goes through. The
    # operator can diff them later if they actually had local edits;
    # for the typical case (someone hand-copied the new file before
    # the pull) the moved version is just a duplicate.
    CONFLICT_BACKUP_DIR="$(mktemp -d -t rentmap-conflict.XXXXXX)"
    CONFLICTS=0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if [ -e "$f" ] && ! git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
            log "moving untracked $f aside (incoming pull adds it)"
            mkdir -p "$CONFLICT_BACKUP_DIR/$(dirname "$f")"
            mv "$f" "$CONFLICT_BACKUP_DIR/$f"
            CONFLICTS=$((CONFLICTS + 1))
        fi
    done < <(git diff --name-only --diff-filter=A "$LOCAL_REF..$REMOTE_REF" 2>/dev/null || true)
    if [ "$CONFLICTS" -gt 0 ]; then
        log "moved $CONFLICTS conflicting file(s) to $CONFLICT_BACKUP_DIR (keep for diff if you had local edits)"
    else
        rmdir "$CONFLICT_BACKUP_DIR" 2>/dev/null || true
    fi

    git pull --ff-only
fi

# -------- 3. Restore (via trap, but also do it eagerly so the docker
# step sees the files) --------
if [ "$BACKUP_COUNT" -gt 0 ]; then
    log "restoring $BACKUP_COUNT web file(s)"
    cp "$BACKUP_DIR"/* web/
fi

# -------- 4. Rebuild + restart --------
if [ "$DO_BUILD" -eq 1 ]; then
    log "docker compose build"
    docker compose build
fi

log "docker compose up -d (recreates containers with new image/scripts)"
docker compose up -d

# -------- 5. Brief health check --------
sleep 3
log "running containers:"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'

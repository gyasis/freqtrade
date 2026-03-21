#!/usr/bin/env bash
# TaskCompleted Hook - Auto-checkpoint and sync GitHub issues after task completion
# Claude Code: exit 0 = success

# Master kill-switch
if [ "${DEV_KID_HOOKS_ENABLED:-true}" = "false" ]; then
    exit 0
fi

read -r EVENT_DATA || true

echo "$(date -Iseconds) TaskCompleted" >> .claude/activity_stream.md 2>/dev/null || true

if ! command -v dev-kid &>/dev/null; then
    exit 0
fi

# Auto-sync GitHub issues if enabled
if [ "$DEV_KID_AUTO_SYNC_GITHUB" = "true" ] && [ -f tasks.md ]; then
    MODIFIED=$(git diff --name-only tasks.md 2>/dev/null || true)
    STAGED=$(git diff --cached --name-only tasks.md 2>/dev/null || true)
    if [ -n "$MODIFIED" ] || [ -n "$STAGED" ]; then
        dev-kid gh-sync 2>/dev/null || true
    fi
fi

# Auto-checkpoint if enabled
if [ "$DEV_KID_AUTO_CHECKPOINT" = "true" ]; then
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        dev-kid checkpoint "[TASK-COMPLETE] Auto-checkpoint" 2>/dev/null || true
    fi
fi

exit 0

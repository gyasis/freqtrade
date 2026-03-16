#!/usr/bin/env bash
# UserPromptSubmit Hook - Inject project context before prompt processing
# Claude Code: stdout text is injected as context into the prompt

# Master kill-switch
if [ "${DEV_KID_HOOKS_ENABLED:-true}" = "false" ]; then
    exit 0
fi

# Read stdin safely
read -r EVENT_DATA || true

CONTEXT=""

# Current git branch
if git rev-parse --git-dir >/dev/null 2>&1; then
    BRANCH=$(git branch --show-current 2>/dev/null || echo "detached")
    CONTEXT+="📍 Branch: $BRANCH\n"
fi

# Constitution rules
if [ -f memory-bank/shared/.constitution.md ]; then
    SUMMARY=$(head -n 20 memory-bank/shared/.constitution.md | grep -E "^##|^-" | head -n 5 2>/dev/null || true)
    [ -n "$SUMMARY" ] && CONTEXT+="📜 Constitution:\n$SUMMARY\n"
fi

# Task progress
if [ -f tasks.md ]; then
    TOTAL=$(grep -c "^- \[.\]" tasks.md 2>/dev/null || echo "0")
    COMPLETED=$(grep -c "^- \[x\]" tasks.md 2>/dev/null || echo "0")
    [ "$TOTAL" -gt 0 ] 2>/dev/null && CONTEXT+="📊 Tasks: $COMPLETED/$TOTAL complete\n" || true
fi

# Current wave
if [ -f execution_plan.json ]; then
    WAVE=$(jq -r '.execution_plan.current_wave // empty' execution_plan.json 2>/dev/null || true)
    [ -n "$WAVE" ] && CONTEXT+="🌊 Wave: $WAVE\n"
fi

# Output context (injected into prompt by Claude Code)
if [ -n "$CONTEXT" ]; then
    printf "\n---\n🤖 Project Context:\n${CONTEXT}---\n"
fi

exit 0

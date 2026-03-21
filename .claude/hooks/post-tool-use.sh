#!/usr/bin/env bash
# PostToolUse Hook - Format and lint after file edits
# Claude Code: exit 0 = success, non-zero = hook error

# Master kill-switch
if [ "${DEV_KID_HOOKS_ENABLED:-true}" = "false" ]; then
    exit 0
fi

# Read stdin safely (never use set -e with read)
read -r EVENT_DATA || true

# Extract tool name and file path from JSON input
TOOL_NAME=$(echo "$EVENT_DATA" | grep -oP '"tool_name":\s*"\K[^"]+' 2>/dev/null || true)
FILE_PATH=$(echo "$EVENT_DATA" | grep -oP '"path":\s*"\K[^"]+' 2>/dev/null || true)

# Only process Edit/Write/MultiEdit tools
if [[ "$TOOL_NAME" != "Edit" && "$TOOL_NAME" != "Write" && "$TOOL_NAME" != "MultiEdit" ]]; then
    exit 0
fi

[ -z "$FILE_PATH" ] && exit 0

# Auto-format Python files
if [[ "$FILE_PATH" == *.py ]]; then
    command -v black &>/dev/null && black "$FILE_PATH" 2>/dev/null || true
    command -v isort &>/dev/null && isort "$FILE_PATH" 2>/dev/null || true
fi

# Auto-format JS/TS files
if [[ "$FILE_PATH" =~ \.(js|ts|jsx|tsx)$ ]]; then
    command -v prettier &>/dev/null && prettier --write "$FILE_PATH" 2>/dev/null || true
fi

# Auto-format Bash scripts
if [[ "$FILE_PATH" == *.sh ]]; then
    command -v shfmt &>/dev/null && shfmt -w "$FILE_PATH" 2>/dev/null || true
fi

exit 0

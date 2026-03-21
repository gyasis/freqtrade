#!/usr/bin/env bash
# PostToolUseFailure Hook - Error recovery and logging
# NON-BLOCKING: Logs errors for debugging

set -e

# Read stdin (contains error metadata)
read -r EVENT_DATA

# Extract error information
TOOL_NAME=$(echo "$EVENT_DATA" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "unknown")
ERROR_MSG=$(echo "$EVENT_DATA" | grep -o '"error":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "unknown")

# Log to activity stream
echo "" >> .claude/activity_stream.md
echo "### ❌ Tool Failure - $(date +%Y-%m-%d\ %H:%M:%S)" >> .claude/activity_stream.md
echo "**Tool:** $TOOL_NAME" >> .claude/activity_stream.md
echo "**Error:** $ERROR_MSG" >> .claude/activity_stream.md

# Log to error log
mkdir -p .claude/logs
ERROR_LOG=".claude/logs/tool_failures.jsonl"
echo "{\"timestamp\":\"$(date -Iseconds)\",\"tool\":\"$TOOL_NAME\",\"error\":\"$ERROR_MSG\"}" >> "$ERROR_LOG"

# Update system bus (if available)
if [ -f .claude/system_bus.json ]; then
    python3 << 'PYTHON'
import json
from pathlib import Path
from datetime import datetime
import sys

bus_file = Path('.claude/system_bus.json')
if bus_file.exists():
    with open(bus_file) as f:
        bus = json.load(f)

    event_data = sys.stdin.read()

    bus['events'].append({
        'timestamp': datetime.now().isoformat(),
        'agent': 'post-tool-use-failure-hook',
        'event_type': 'tool_failure',
        'tool_name': event_data,
        'logged': True
    })

    with open(bus_file, 'w') as f:
        json.dump(bus, f, indent=2)
PYTHON
fi

# Return success (non-blocking)
echo '{"status": "success", "message": "Error logged for debugging"}'
exit 0

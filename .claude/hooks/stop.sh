#!/usr/bin/env bash
# Stop Hook - Test enforcement before session end
# BLOCKING: Prevents session stop if tests are failing

set -e

# Read stdin
read -r EVENT_DATA

# Check if we're in a Python project
if [ ! -f "setup.py" ] && [ ! -f "pyproject.toml" ]; then
    echo '{"blocked": false, "message": "Not a Python project - skipping test enforcement"}'
    exit 0
fi

# Check if pytest is available
if ! command -v pytest &> /dev/null; then
    echo '{"blocked": false, "message": "pytest not installed - skipping test enforcement"}'
    exit 0
fi

# Check for DEV_KID_ENFORCE_TESTS environment variable
if [ "$DEV_KID_ENFORCE_TESTS" != "true" ]; then
    echo '{"blocked": false, "message": "Test enforcement disabled (set DEV_KID_ENFORCE_TESTS=true to enable)"}'
    exit 0
fi

# Run tests
echo "🧪 Running tests before session stop..." >&2
if pytest --maxfail=1 --disable-warnings -q 2>&1; then
    echo '{"blocked": false, "message": "✅ All tests passed - session stop allowed"}'
    exit 0
else
    echo '{"blocked": true, "message": "❌ BLOCKED: Tests are failing - fix tests before stopping session", "severity": "high"}'
    exit 1
fi

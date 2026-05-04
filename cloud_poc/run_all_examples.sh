#!/usr/bin/env bash
set -u

BROKER_URL="${BROKER_URL:?BROKER_URL must be set}"
EXAMPLES_DIR="${EXAMPLES_DIR:-examples}"
LOG_DIR="${LOG_DIR:-cloud_poc/example-logs}"
mkdir -p "$LOG_DIR"

results=()
for ex in "$EXAMPLES_DIR"/*.py; do
    name=$(basename "$ex" .py)
    log="$LOG_DIR/$name.log"
    echo "=== $name ==="
    timeout 120 python cloud_poc/run_example.py "$BROKER_URL" "$ex" \
        > "$log" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        results+=("PASS  $name")
        echo "  PASS"
    else
        results+=("FAIL($rc)  $name")
        echo "  FAIL (rc=$rc) — see $log"
        tail -n 20 "$log" | sed 's/^/    /'
    fi
done

echo
echo "=== Summary ==="
for r in "${results[@]}"; do
    echo "$r"
done

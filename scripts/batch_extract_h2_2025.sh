#!/usr/bin/env bash
# Batch LLM extraction for CSI 2026 news window (2025 H2).
set -euo pipefail
cd "$(dirname "$0")/.."

SINCE="${1:-2025-07-01}"
UNTIL="${2:-2026-01-01}"
BATCH="${3:-200}"
MOCK="${MOCK:-0}"
EXTRA=()
if [[ "$MOCK" == "1" ]]; then
  EXTRA+=(--mock)
fi

round=0
while true; do
  round=$((round + 1))
  echo "=== batch $round (limit=$BATCH) ==="
  out=$(python3 scripts/run_news_extraction.py --since "$SINCE" --until "$UNTIL" --limit "$BATCH" --sleep 0.1 "${EXTRA[@]}" 2>&1) || true
  echo "$out" | tail -5
  if echo "$out" | grep -q "No pending articles"; then
    echo "All H2 2025 articles extracted."
    break
  fi
  if echo "$out" | grep -q "Done: ok=0 failed="; then
    echo "Batch failed entirely; stopping."
    exit 1
  fi
done

python3 scripts/backfill_news_2025.py --validate-only

#!/bin/bash
# Install launchd agents for teststock news pipeline (macOS)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

for name in teststock-news-flash teststock-news-daily teststock-news-processing; do
  src="${ROOT}/scripts/launchd/ai.jingxuan.${name}.plist"
  dst="${AGENTS_DIR}/ai.jingxuan.${name}.plist"
  cp "$src" "$dst"
  launchctl bootout "gui/$(id -u)/ai.jingxuan.${name}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  launchctl enable "gui/$(id -u)/ai.jingxuan.${name}" 2>/dev/null || true
  echo "Installed ai.jingxuan.${name}"
done

echo "Done. Check: launchctl list | grep teststock-news"

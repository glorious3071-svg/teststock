"""Run annual-direction MCP server: python -m mcp_servers.annual_direction"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp_servers.annual_direction.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()

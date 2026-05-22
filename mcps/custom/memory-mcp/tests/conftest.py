"""Put the MCP root (where server.py lives) on sys.path so `import server` works."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

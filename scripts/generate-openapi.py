#!/usr/bin/env python3
"""Write the description of the public API (proxy/api/public/openapi.json)
from the listed routes alone. Run it from anywhere, with the proxy's venv,
after changing a public route:

    proxy/venv/bin/python scripts/generate-openapi.py

A test fails while the committed file differs from what this writes.
"""

import json
import sys
from pathlib import Path

PROXY = Path(__file__).resolve().parent.parent / "proxy"
sys.path.insert(0, str(PROXY))

from api.public.description import build

target = PROXY / "api" / "public" / "openapi.json"
target.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {target}")

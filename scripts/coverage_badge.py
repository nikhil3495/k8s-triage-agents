"""Emit a shields.io endpoint JSON from a coverage.xml (used by CI)."""

import json
import sys
import xml.etree.ElementTree as ET

rate = float(ET.parse(sys.argv[1]).getroot().get("line-rate", 0)) * 100  # noqa: S314
color = "brightgreen" if rate >= 90 else "green" if rate >= 80 else "orange"
print(
    json.dumps({"schemaVersion": 1, "label": "coverage", "message": f"{rate:.0f}%", "color": color})
)

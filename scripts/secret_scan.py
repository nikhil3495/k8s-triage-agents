"""Pre-push secret scan over files tracked/staged by git (gitleaks also runs in CI)."""

from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "github_token": re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "gcp_service_account": re.compile(r'"type":\s*"service_account"'),
    "slack_token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
}


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    hits = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (IsADirectoryError, FileNotFoundError):
            continue
        for name, rx in PATTERNS.items():
            if rx.search(text):
                hits.append(f"{f}: {name}")
    for h in hits:
        print("SECRET?", h)
    print(f"scanned {len(files)} files, {len(hits)} findings")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

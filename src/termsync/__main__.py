"""`python -m termsync` — what makes the project runnable at all (S3)."""

from __future__ import annotations

import sys

from termsync.cli import main

if __name__ == "__main__":
    sys.exit(main())

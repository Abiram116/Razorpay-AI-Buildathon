"""The one command to see this project running.

    uv run run.py            # seeds demo data on first run, then launches the dashboard
    uv run run.py --reset    # wipe and reseed even if data already exists

Seeding (9 cases + AI investigation) only happens once - if merchant.db and
cases.db already exist, it's skipped so re-running this doesn't burn Groq
quota for nothing. Use --reset to force a fresh seed.

The dashboard is the only long-running process: this script execs into it
directly rather than spawning a subprocess, so there is exactly one process
to stop. Ctrl-C stops it, full stop - nothing lingers in the background.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.config import load_settings  # noqa: E402


def main() -> int:
    reset = "--reset" in sys.argv
    settings = load_settings(require_razorpay=False)

    already_seeded = settings.paths.case_db.exists() and settings.paths.merchant_db.exists()
    if reset or not already_seeded:
        reason = "--reset requested" if reset else "no demo data found yet"
        print(f"[run.py] Seeding demo data ({reason})...", flush=True)
        seed_cmd = [sys.executable, str(ROOT / "scripts" / "seed_merchant_db.py")]
        if reset:
            seed_cmd.append("--reset")
        result = subprocess.run(seed_cmd)
        if result.returncode != 0:
            print("[run.py] Seeding failed - not starting the dashboard.", file=sys.stderr, flush=True)
            return result.returncode
    else:
        print("[run.py] Demo data already exists - skipping seed (use --reset to rebuild).", flush=True)

    print("\n[run.py] Starting dashboard at http://localhost:8501 - Ctrl-C to stop.\n")
    dashboard_argv = [sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "app.py")]
    # os.execvp() replaces this process's image without flushing Python's
    # buffered stdout first - every print() above would silently vanish
    # whenever stdout isn't a live terminal (redirected to a file, piped,
    # etc.) without this explicit flush.
    sys.stdout.flush()
    os.execvp(sys.executable, dashboard_argv)


if __name__ == "__main__":
    raise SystemExit(main())

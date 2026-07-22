"""Pick a random unused account UID from account-backups.json and print it.

Shares the candidate-filtering logic with reset-zcode-device.py so the two
never drift apart. The only difference from --auto-switch is the final pick:
this script chooses uniformly at random instead of deterministically.

Filter criteria (implemented in reset_zcode_device.collect_candidates):
  1. Plan not expired (endsAt > now)
  2. Not used today (checkedAt < today's local midnight)
  3. Not the current account (identified via the active provider's JWT)

Exits non-zero with a single-line reason on stderr when no candidate is
available, so the calling batch can tell "no account" apart from a real uid.
"""
import importlib.util
import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# Import the shared logic from the sibling main script. The filename contains
# a hyphen so a plain `import` won't work; load it via importlib instead.
# The main script is guarded by `if __name__ == "__main__"`, so loading it
# here is side-effect free.
_spec = importlib.util.spec_from_file_location(
    "reset_zcode_device", SCRIPT_DIR / "reset-zcode-device.py"
)
rzd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rzd)  # type: ignore[union-attr]


def _fail(reason: str) -> None:
    """Print reason to stderr and exit non-zero (no stdout produced)."""
    print(reason, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    backups = rzd._load_backups_silent()
    if backups is None:
        _fail(f"backups file not found: {rzd.ACCOUNT_BACKUP_PATH}")
    if not backups:
        _fail("no accounts in backups file")

    candidates = rzd.collect_candidates(backups)
    if not candidates:
        _fail("no available accounts (all expired or already used today)")

    # Each candidate: (ends_ts, checked_ts, uid, backup). Pick a uid at random.
    _, _, uid, _ = random.choice(candidates)
    print(uid)


if __name__ == "__main__":
    main()

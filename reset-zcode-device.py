#!/usr/bin/env python3
"""ZCode Device ID Reset Tool.

Resets the ZCode telemetry device ID by:
1. Reading the current deviceMid
2. Terminating running ZCode processes
3. Disconnecting the account (removing OAuth credentials)
4. Clearing plan provider API keys in config.json
5. Updating coding-plan-cache.json to mark plans as unavailable
6. Removing providerFamilyDomain from setting.json
7. Deleting the telemetry-state.json file
8. Relaunching ZCode so a fresh deviceMid is generated
9. Verifying the new deviceMid

Standard library only - no dependencies to install.

Usage:
    python reset-zcode-device.py              # run the full reset
    python reset-zcode-device.py --dry-run    # show what would happen, change nothing
    python reset-zcode-device.py --no-launch  # reset without relaunching ZCode
    python reset-zcode-device.py --status     # query plan status and balance
    python reset-zcode-device.py --backup     # backup current account
    python reset-zcode-device.py --list-accounts  # list saved accounts
    python reset-zcode-device.py --switch <MID>   # switch to saved account
    python reset-zcode-device.py --auto-switch    # auto switch to unused account
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import base64
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding for Chinese characters
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEMETRY_PATH = Path.home() / ".zcode" / "v2" / "telemetry-state.json"
CREDENTIALS_PATH = Path.home() / ".zcode" / "v2" / "credentials.json"
CONFIG_PATH = Path.home() / ".zcode" / "v2" / "config.json"
CODING_PLAN_CACHE_PATH = Path.home() / ".zcode" / "v2" / "coding-plan-cache.json"
SETTING_PATH = Path.home() / ".zcode" / "v2" / "setting.json"
PLAN_STATUS_CACHE_PATH = Path.home() / ".zcode" / "v2" / "plan-status-cache.json"
ACCOUNT_BACKUP_PATH = Path(__file__).parent / "account-backups.json"
LOGS_DIR = Path.home() / ".zcode" / "v2" / "logs"

BILLING_API_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.3.6"
PLAN_PROVIDER_KEY = "builtin:bigmodel-start-plan"

# Credential keys belonging to the Zhipu AI / BigModel OAuth login session.
# Removing these logs the user out while preserving bot credentials,
# remote-control keys, and custom provider configs.
ZHIPU_CREDENTIAL_KEYS = [
    "oauth:bigmodel:access_token",
    "oauth:bigmodel:user_info",
    "oauth:active_provider",
    "zcodejwttoken",
]

# Executable names we look for when killing processes.
PROCESS_NAMES = ["ZCode", "zcode", "zcode-helper", "zcode-cli"]

# Candidate install locations for the ZCode GUI executable (Windows).
ZCODE_WIN_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "zcode" / "ZCode.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "zcode" / "ZCode.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "zcode" / "ZCode.exe",
]

# Seconds to wait after killing processes / launching ZCode.
KILL_WAIT_SEC = 3
LAUNCH_WAIT_SEC = 10


# ---------------------------------------------------------------------------
# Terminal color helpers (ANSI; gracefully no-op when unsupported)
# ---------------------------------------------------------------------------

_COLOR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# Enable ANSI escape code processing on Windows.
if _COLOR_ENABLED and os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        _COLOR_ENABLED = False


def _color(code: str, text: str) -> str:
    if not _COLOR_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def c_cyan(text: str) -> str:
    return _color("36", text)


def c_yellow(text: str) -> str:
    return _color("33", text)


def c_green(text: str) -> str:
    return _color("32", text)


def c_red(text: str) -> str:
    return _color("31", text)


def c_gray(text: str) -> str:
    return _color("90", text)


def banner(text: str) -> None:
    print(c_cyan("=" * 40))
    print(c_cyan(text))
    print(c_cyan("=" * 40))


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def read_device_mid(path: Path) -> str:
    """Return the current deviceMid from the telemetry file, or 'N/A'."""
    if not path.exists():
        return "N/A"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to parse telemetry file: {exc}"))
        return "N/A"
    return str(data.get("deviceMid", "N/A"))


def save_account_backup(dry_run: bool) -> dict | None:
    """Save current account credentials and plan info for account switching later.

    Uses user_id (from JWT) as the backup key.
    Returns the saved backup dict, or None on failure.
    """
    device_mid = read_device_mid(TELEMETRY_PATH)
    api_key = get_api_key()

    # Get user_id from JWT token
    user_id = get_user_id_from_token(api_key) if api_key else None
    if not user_id:
        print(c_red("   Cannot determine user_id from token."))
        return None

    # Read OAuth credentials
    oauth_creds = {}
    if CREDENTIALS_PATH.exists():
        try:
            all_creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            oauth_creds = {k: v for k, v in all_creds.items() if k in ZHIPU_CREDENTIAL_KEYS}
        except (OSError, ValueError) as exc:
            print(c_red(f"   Failed to read credentials: {exc}"))

    if not oauth_creds:
        print(c_gray("   No OAuth credentials to backup."))
        return None

    # Query plan status - try logs first (no network), then API
    plan_info = {}

    # Try reading from local logs
    log_plan = read_plan_from_logs()
    if log_plan:
        starts_at = log_plan.get("startsAt", 0)
        ends_at = log_plan.get("endsAt", 0)
        checked_at = log_plan.get("checkedAt", 0)
        plan_info = {
            "planName": log_plan.get("planName", ""),
            "planStatus": log_plan.get("planStatus", ""),
            "startsAt": datetime.fromtimestamp(starts_at).strftime("%Y-%m-%d %H:%M:%S") if starts_at else "",
            "endsAt": datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d %H:%M:%S") if ends_at else "",
            "checkedAt": datetime.fromtimestamp(checked_at).strftime("%Y-%m-%d %H:%M:%S") if checked_at else "",
            "source": "log",
        }
        ends_str = datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d") if ends_at else "N/A"
        print(c_gray(f"   Plan: {log_plan.get('planName', 'N/A')}, expires: {ends_str} (from log)"))

    # Fallback to API if log not found
    if not plan_info and api_key:
        data = query_plan_status(api_key)
        if data and data.get("code", -1) == 0:
            plans = data.get("data", {}).get("plans", [])
            server_time = data.get("data", {}).get("server_time", 0)
            if plans:
                plan = plans[0]
                starts_at = plan.get("starts_at", 0)
                ends_at = plan.get("ends_at", 0)
                plan_info = {
                    "planName": plan.get("name", ""),
                    "planStatus": plan.get("status", ""),
                    "startsAt": datetime.fromtimestamp(starts_at).strftime("%Y-%m-%d %H:%M:%S") if starts_at else "",
                    "endsAt": datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d %H:%M:%S") if ends_at else "",
                    "checkedAt": datetime.fromtimestamp(server_time).strftime("%Y-%m-%d %H:%M:%S") if server_time else "",
                    "source": "api",
                }
                ends_str = datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d") if ends_at else "N/A"
                print(c_gray(f"   Plan: {plan.get('name', 'N/A')}, expires: {ends_str} (from api)"))

    if not plan_info:
        print(c_yellow("   Warning: No plan info found. Backup saved without plan data."))

    # Read existing backups
    backups = {}
    if ACCOUNT_BACKUP_PATH.exists():
        try:
            backups = json.loads(ACCOUNT_BACKUP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backups = {}

    backup = {
        "userId": user_id,
        "deviceMid": device_mid,
        "credentials": oauth_creds,
        "plan": plan_info,
        "savedAt": datetime.now().strftime("%Y-%m-%d"),
    }

    # Use user_id as key, upsert
    backups[user_id] = backup

    if dry_run:
        print(c_gray(f"   [dry-run] would save backup for user: {user_id}"))
        print(c_gray(f"   [dry-run] credentials: {list(oauth_creds.keys())}"))
        return backup

    try:
        ACCOUNT_BACKUP_PATH.write_text(
            json.dumps(backups, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(c_green(f"   Saved account backup: {ACCOUNT_BACKUP_PATH}"))
        print(c_gray(f"   user_id: {user_id}"))
        print(c_gray(f"   credentials: {list(oauth_creds.keys())}"))
        return backup
    except OSError as exc:
        print(c_red(f"   Failed to save backup: {exc}"))
        return None


def list_account_backups() -> int:
    """List all saved account backups."""
    if not ACCOUNT_BACKUP_PATH.exists():
        print(c_gray("   No saved accounts found."))
        return 0

    try:
        backups = json.loads(ACCOUNT_BACKUP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read backups: {exc}"))
        return 1

    if not backups:
        print(c_gray("   No saved accounts found."))
        return 0

    api_key = get_api_key()
    current_uid = get_user_id_from_token(api_key) if api_key else None

    for i, (uid, backup) in enumerate(backups.items(), 1):
        saved_at = backup.get("savedAt", "")
        is_current = " (current)" if uid == current_uid else ""
        device_mid = backup.get("deviceMid", "N/A")
        creds = backup.get("credentials", {})
        has_token = "token" if creds.get("zcodejwttoken") else "no-token"

        plan = backup.get("plan", {})
        plan_name = plan.get("planName", "")
        ends_at = plan.get("endsAt", "")
        checked_at = plan.get("checkedAt", "")

        print(c_green(f"   [{i}] user: {uid}{is_current}"))
        print(c_gray(f"       device: {device_mid}"))
        print(c_gray(f"       saved: {saved_at}, auth: {has_token}"))
        if plan_name:
            print(c_gray(f"       plan: {plan_name}, expires: {ends_at}, checked: {checked_at}"))

    print()
    print(c_gray(f"   Total: {len(backups)} account(s)"))
    return 0


def switch_account(uid: str, dry_run: bool) -> int:
    """Restore credentials from a saved backup to switch accounts."""
    if not ACCOUNT_BACKUP_PATH.exists():
        print(c_red("   No account backups found."))
        return 1

    try:
        backups = json.loads(ACCOUNT_BACKUP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read backups: {exc}"))
        return 1

    # Support 'last' keyword: pick the most recently saved
    if uid == "last":
        if not backups:
            print(c_red("   No backups available."))
            return 1
        uid = max(backups, key=lambda k: backups[k].get("savedAt", 0))
        print(c_gray(f"   Using most recent: {uid}"))

    if uid not in backups:
        print(c_red(f"   Account not found: {uid}"))
        print(c_gray(f"   Available: {list(backups.keys())}"))
        return 1

    backup = backups[uid]
    saved_creds = backup.get("credentials", {})
    if not saved_creds:
        print(c_red("   Backup has no credentials."))
        return 1

    # Read current credentials
    current_creds = {}
    if CREDENTIALS_PATH.exists():
        try:
            current_creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(c_red(f"   Failed to read current credentials: {exc}"))
            return 1

    # Merge: overwrite OAuth keys, keep others
    merged = {**current_creds, **saved_creds}

    print(c_gray(f"   Restoring {len(saved_creds)} credential key(s):"))
    for k in saved_creds:
        print(c_gray(f"     - {k}"))

    if dry_run:
        print(c_gray("   [dry-run] no changes made."))
        return 0

    # Kill ZCode first
    print(c_yellow("   Stopping ZCode..."))
    kill_zcode_processes(False)
    time.sleep(KILL_WAIT_SEC)

    # Write credentials
    try:
        CREDENTIALS_PATH.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(c_green("   Credentials restored."))
    except OSError as exc:
        print(c_red(f"   Failed to write credentials: {exc}"))
        return 1

    # Delete telemetry so ZCode re-registers
    if TELEMETRY_PATH.exists():
        TELEMETRY_PATH.unlink()
        print(c_gray("   Deleted telemetry-state.json (will regenerate)."))

    # Relaunch ZCode
    print(c_yellow("   Starting ZCode..."))
    if launch_zcode(False):
        print(c_green(f"   Switched to account: {uid}"))
    else:
        print(c_yellow("   Credentials restored, but ZCode launch failed. Start manually."))

    return 0


def auto_switch_account(dry_run: bool) -> int:
    """Auto switch to an unused account with valid plan.

    Criteria:
    1. Plan not expired (endsAt > now)
    2. Not used today (checkedAt is not today)
    """
    if not ACCOUNT_BACKUP_PATH.exists():
        print(c_red("   No account backups found."))
        return 1

    try:
        backups = json.loads(ACCOUNT_BACKUP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read backups: {exc}"))
        return 1

    if not backups:
        print(c_red("   No backups available."))
        return 1

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    api_key = get_api_key()
    current_uid = get_user_id_from_token(api_key) if api_key else None
    candidates = []

    for uid, backup in backups.items():
        if uid == current_uid:
            continue

        plan = backup.get("plan", {})
        if not plan:
            continue

        ends_at_str = plan.get("endsAt", "")
        if ends_at_str:
            try:
                ends_at = datetime.strptime(ends_at_str, "%Y-%m-%d %H:%M:%S")
                if ends_at < now:
                    continue
            except ValueError:
                continue

        checked_at_str = plan.get("checkedAt", "")
        if checked_at_str:
            try:
                checked_at = datetime.strptime(checked_at_str, "%Y-%m-%d %H:%M:%S")
                if checked_at >= today_start:
                    continue
            except ValueError:
                continue

        candidates.append((uid, backup))

    if not candidates:
        print(c_yellow("   No available accounts found."))
        print(c_gray("   Requirements: plan not expired, not used today."))
        return 1

    best_uid, best_backup = candidates[0]
    plan = best_backup.get("plan", {})
    ends_str = plan.get("endsAt", "N/A")

    print(c_green(f"   Found account: {best_uid}"))
    print(c_gray(f"   Plan: {plan.get('planName', 'N/A')}, expires: {ends_str}"))

    return switch_account(best_uid, dry_run)


def disconnect_account(dry_run: bool) -> bool:
    """Remove Zhipu AI OAuth credentials to log out while preserving custom providers.

    Reads credentials.json, strips the Zhipu-specific keys, and writes back.
    Returns True on success (or dry-run).
    """
    if not CREDENTIALS_PATH.exists():
        print(c_gray("   credentials.json not found, already disconnected."))
        return True

    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read credentials: {exc}"))
        return False

    keys_to_remove = [k for k in ZHIPU_CREDENTIAL_KEYS if k in data]
    if not keys_to_remove:
        print(c_gray("   No Zhipu AI credentials found, already disconnected."))
        return True

    if dry_run:
        for k in keys_to_remove:
            print(c_gray(f"   [dry-run] would remove key: {k}"))
        return True

    for k in keys_to_remove:
        del data[k]

    try:
        CREDENTIALS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(c_green(f"   Removed {len(keys_to_remove)} Zhipu AI credential key(s)."))
        print(c_gray("   Custom providers and other credentials preserved."))
        print(c_gray("   ZCode will detect the logout and may restart."))
        return True
    except OSError as exc:
        print(c_red(f"   Failed to write credentials: {exc}"))
        return False


def clear_config_api_keys(dry_run: bool) -> bool:
    """Clear API keys for all builtin plan providers in config.json.

    Sets apiKey to empty string, enabled to false, and adds systemDisabledReason.
    Returns True on success (or dry-run).
    """
    if not CONFIG_PATH.exists():
        print(c_gray("   config.json not found, skipping."))
        return True

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read config.json: {exc}"))
        return False

    providers = data.get("provider", {})
    plan_providers = [k for k in providers if k.startswith("builtin:") and k.endswith("-plan")]

    if not plan_providers:
        print(c_gray("   No builtin plan providers found, skipping."))
        return True

    if dry_run:
        for k in plan_providers:
            print(c_gray(f"   [dry-run] would clear apiKey for: {k}"))
        return True

    modified = 0
    for key in plan_providers:
        provider = providers[key]
        options = provider.get("options", {})
        if options.get("apiKey"):
            options["apiKey"] = ""
            modified += 1
        provider["enabled"] = False
        provider["systemDisabledReason"] = "coding_plan_not_authenticated"

    if modified:
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(c_green(f"   Cleared {modified} plan provider API key(s)."))
            return True
        except OSError as exc:
            print(c_red(f"   Failed to write config.json: {exc}"))
            return False
    else:
        print(c_gray("   No API keys to clear."))
        return True


def update_coding_plan_cache(dry_run: bool) -> bool:
    """Update coding-plan-cache.json to mark all plan providers as unavailable.

    Returns True on success (or dry-run).
    """
    if not CODING_PLAN_CACHE_PATH.exists():
        print(c_gray("   coding-plan-cache.json not found, skipping."))
        return True

    try:
        data = json.loads(CODING_PLAN_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read coding-plan-cache.json: {exc}"))
        return False

    items = data.get("entryStatus", {}).get("items", {})
    plan_items = {k: v for k, v in items.items() if k.startswith("builtin:") and k.endswith("-plan")}

    if not plan_items:
        print(c_gray("   No plan entries in cache, skipping."))
        return True

    if dry_run:
        for k in plan_items:
            print(c_gray(f"   [dry-run] would mark {k} as unavailable"))
        return True

    modified = 0
    for key, item in plan_items.items():
        if item.get("status") != "unavailable" or item.get("reason") != "coding_plan_not_authenticated":
            item["status"] = "unavailable"
            item["reason"] = "coding_plan_not_authenticated"
            modified += 1

    if modified:
        data["entryStatus"]["updatedAt"] = int(time.time() * 1000)
        try:
            CODING_PLAN_CACHE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(c_green(f"   Updated {modified} cache entry(ies)."))
            return True
        except OSError as exc:
            print(c_red(f"   Failed to write coding-plan-cache.json: {exc}"))
            return False
    else:
        print(c_gray("   Cache entries already up to date."))
        return True


def clear_provider_family_domain(dry_run: bool) -> bool:
    """Remove providerFamilyDomain from setting.json.

    Returns True on success (or dry-run).
    """
    if not SETTING_PATH.exists():
        print(c_gray("   setting.json not found, skipping."))
        return True

    try:
        data = json.loads(SETTING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(c_red(f"   Failed to read setting.json: {exc}"))
        return False

    if "providerFamilyDomain" not in data:
        print(c_gray("   providerFamilyDomain not found, already cleared."))
        return True

    if dry_run:
        print(c_gray(f"   [dry-run] would remove providerFamilyDomain: {data['providerFamilyDomain']}"))
        return True

    del data["providerFamilyDomain"]

    try:
        SETTING_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(c_green("   Removed providerFamilyDomain."))
        return True
    except OSError as exc:
        print(c_red(f"   Failed to write setting.json: {exc}"))
        return False


def get_api_key() -> str | None:
    """Read the JWT API key from config.json for the active plan provider."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    provider = data.get("provider", {}).get(PLAN_PROVIDER_KEY, {})
    return provider.get("options", {}).get("apiKey") or None


def get_user_id_from_token(api_key: str) -> str | None:
    """Extract user_id from JWT token payload."""
    try:
        parts = api_key.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.b64decode(payload)
        data = json.loads(decoded)
        return str(data.get("user_id", ""))
    except Exception:
        return None


def read_plan_from_logs() -> dict | None:
    """Read the latest plan info from ZCode log files.

    Searches for billing/balance API responses in today's and recent logs.
    Returns plan dict or None if not found.
    """
    if not LOGS_DIR.exists():
        return None

    # Check today's log first, then recent ones
    log_files = sorted(LOGS_DIR.glob("*.log"), reverse=True)

    for log_file in log_files:
        try:
            content = log_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Find the last billing/balance response
        last_pos = content.rfind("billing/balance")
        if last_pos == -1:
            continue

        # Extract JSON from that line
        line_start = content.rfind("\n", 0, last_pos) + 1
        line_end = content.find("\n", last_pos)
        if line_end == -1:
            line_end = len(content)
        line = content[line_start:line_end]

        # Find the JSON object in the line
        json_start = line.find('{"balanceCount')
        if json_start == -1:
            continue

        try:
            data = json.loads(line[json_start:])
            plans = data.get("payload", {}).get("data", {}).get("plans", [])
            server_time = data.get("payload", {}).get("data", {}).get("server_time", 0)
            if plans:
                plan = plans[0]
                return {
                    "planName": plan.get("name", ""),
                    "planStatus": plan.get("status", ""),
                    "startsAt": plan.get("starts_at", 0),
                    "endsAt": plan.get("ends_at", 0),
                    "checkedAt": server_time,
                    "source": "log",
                }
        except (json.JSONDecodeError, KeyError):
            continue

    return None


def query_plan_status(api_key: str) -> dict | None:
    """Call billing/balance API and return parsed response, or None on error."""
    req = urllib.request.Request(
        BILLING_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(c_red(f"   API request failed: {exc}"))
        return None


def display_plan_status(data: dict) -> None:
    """Pretty-print plan info and model balances."""
    inner = data.get("data", {})
    plans = inner.get("plans", [])
    balances = inner.get("balances", [])

    if not plans:
        print(c_yellow("   No active plan found."))
        return

    plan = plans[0]
    ends_at = plan.get("ends_at", 0)
    ends_str = datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d") if ends_at else "N/A"

    print()
    print(c_cyan("   ┌─────────────────────────────────────┐"))
    print(c_cyan("   │") + c_green("  ZCode Plan Status                   ") + c_cyan("│"))
    print(c_cyan("   ├─────────────────────────────────────┤"))
    print(c_cyan("   │") + f"  套餐: {plan.get('name', 'N/A'):<29}" + c_cyan("│"))
    print(c_cyan("   │") + f"  描述: {plan.get('description', 'N/A'):<29}" + c_cyan("│"))
    print(c_cyan("   │") + f"  状态: {plan.get('status', 'N/A'):<29}" + c_cyan("│"))
    print(c_cyan("   │") + f"  到期: {ends_str:<29}" + c_cyan("│"))
    print(c_cyan("   ├─────────────────────────────────────┤"))
    print(c_cyan("   │") + c_yellow("  今日余额                           ") + c_cyan("│"))

    for b in balances:
        name = b.get("show_name", b.get("entitlement_id", "?"))
        used = b.get("used_units", 0)
        total = b.get("total_units", 0)
        remaining = b.get("remaining_units", 0)
        pct = round(remaining / total * 100) if total else 0
        bar_len = 20
        filled = round(pct / 100 * bar_len)
        bar = "#" * filled + "-" * (bar_len - filled)
        line = f"  {name:<14} {remaining:>10,} / {total:<10,} {pct:>3}%"
        print(c_cyan("   |") + line + " " * max(0, 37 - len(line)) + c_cyan("|"))
        print(c_cyan("   |") + f"  [{bar}]" + " " * max(0, 35 - len(bar)) + c_cyan("|"))

    print(c_cyan("   └─────────────────────────────────────┘"))


def save_plan_status(data: dict, dry_run: bool) -> bool:
    """Save plan status to local cache file."""
    if dry_run:
        print(c_gray(f"   [dry-run] would save to {PLAN_STATUS_CACHE_PATH}"))
        return True

    inner = data.get("data", {})
    plans = inner.get("plans", [])
    balances = inner.get("balances", [])

    cache = {
        "updatedAt": int(time.time() * 1000),
        "plan": plans[0] if plans else {},
        "balances": [
            {
                "model": b.get("show_name", ""),
                "used": b.get("used_units", 0),
                "remaining": b.get("remaining_units", 0),
                "total": b.get("total_units", 0),
            }
            for b in balances
        ],
    }

    try:
        PLAN_STATUS_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(c_gray(f"   Cached to {PLAN_STATUS_CACHE_PATH}"))
        return True
    except OSError as exc:
        print(c_red(f"   Failed to write cache: {exc}"))
        return False


def query_and_display_status(dry_run: bool) -> int:
    """Query plan status from API, display, and cache locally."""
    api_key = get_api_key()
    if not api_key:
        print(c_red("   No API key found. Please login to ZCode first."))
        return 1

    print(c_gray("   Querying billing API..."))
    data = query_plan_status(api_key)
    if not data:
        return 1

    if data.get("code", -1) != 0:
        print(c_red(f"   API error: {data.get('msg', 'unknown')}"))
        return 1

    display_plan_status(data)
    save_plan_status(data, dry_run)
    return 0


def kill_zcode_processes(dry_run: bool) -> None:
    """Terminate every known ZCode process."""
    if os.name == "nt":
        # taskkill with fallback across process-name casings.
        for name in PROCESS_NAMES:
            cmd = ["taskkill", "/F", "/IM", f"{name}.exe"]
            if dry_run:
                print(c_gray(f"   [dry-run] would run: {' '.join(cmd)}"))
                continue
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # POSIX fallback: use pkill when available.
        pkill = shutil.which("pkill")
        for name in PROCESS_NAMES:
            if not pkill:
                continue
            cmd = [pkill, "-f", name]
            if dry_run:
                print(c_gray(f"   [dry-run] would run: {' '.join(cmd)}"))
                continue
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_zcode_executable() -> Path | None:
    """Locate the ZCode GUI executable, or None if not found."""
    if os.name == "nt":
        for candidate in ZCODE_WIN_CANDIDATES:
            if candidate.exists():
                return candidate
        return None
    # POSIX: prefer a `zcode` on PATH.
    found = shutil.which("zcode")
    return Path(found) if found else None


def launch_zcode(dry_run: bool) -> bool:
    """Start ZCode detached. Returns True if launched (or dry-run)."""
    exe = find_zcode_executable()
    if exe is None:
        print(c_red("   ZCode executable not found in common paths."))
        print(c_yellow("   Please start ZCode manually, then re-run this script to verify."))
        return False
    print(c_green(f"   Starting: {exe}"))
    if dry_run:
        print(c_gray("   [dry-run] launch skipped"))
        return True
    if os.name == "nt":
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: don't tie lifetime to this shell.
        subprocess.Popen(
            [str(exe)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=0x00000008 | 0x00000200,
        )
    else:
        subprocess.Popen(
            [str(exe)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    return True


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset the ZCode telemetry device ID.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without killing, deleting, or launching anything",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="do not (re)launch ZCode after resetting",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="query plan status and balance, then exit",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="list saved account backups, then exit",
    )
    parser.add_argument(
        "--switch",
        metavar="UID",
        help="switch to a saved account by user_id (use 'last' for most recent)",
    )
    parser.add_argument(
        "--auto-switch",
        action="store_true",
        help="auto switch to an unused account with valid plan",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="backup current account credentials and plan info, then exit",
    )
    args = parser.parse_args()

    banner("ZCode Device ID Reset Tool")

    # --status: query plan and exit early
    if args.status:
        print(c_yellow("Querying plan status..."))
        return query_and_display_status(args.dry_run)

    # --list-accounts: show saved backups and exit
    if args.list_accounts:
        print(c_yellow("Saved accounts:"))
        return list_account_backups()

    # --switch: restore credentials from backup
    if args.switch:
        print(c_yellow(f"Switching to account: {args.switch}"))
        return switch_account(args.switch, args.dry_run)

    # --auto-switch: find and switch to an unused account
    if args.auto_switch:
        print(c_yellow("Auto-switching to available account..."))
        return auto_switch_account(args.dry_run)

    # --backup: save current account info and exit
    if args.backup:
        print(c_yellow("Backing up current account..."))
        result = save_account_backup(args.dry_run)
        return 0 if result else 1

    if args.dry_run:
        print(c_yellow("DRY RUN - no changes will be made"))
    print()

    # Step 1: read current deviceMid and backup OAuth credentials
    print(c_yellow("[1/9] Reading current deviceMid and saving account backup..."))
    old_mid = read_device_mid(TELEMETRY_PATH)
    if TELEMETRY_PATH.exists():
        print(c_green(f"   Current deviceMid: {old_mid}"))
    else:
        print(c_gray("   telemetry-state.json not found."))
    save_account_backup(args.dry_run)
    print()

    # Step 2: terminate ZCode processes
    print(c_yellow("[2/9] Terminating all zcode processes..."))
    kill_zcode_processes(args.dry_run)
    if not args.dry_run:
        time.sleep(KILL_WAIT_SEC)
    print(c_green("   Done."))
    print()

    # Step 3: disconnect account (ZCode is already dead, no auto-restart)
    print(c_yellow("[3/9] Disconnecting account..."))
    if not disconnect_account(args.dry_run):
        print(c_red("   Failed to disconnect account. Aborting."))
        return 1
    print()

    # Step 4: clear plan provider API keys in config.json
    print(c_yellow("[4/9] Clearing plan provider API keys..."))
    if not clear_config_api_keys(args.dry_run):
        print(c_red("   Failed to clear API keys. Aborting."))
        return 1
    print()

    # Step 5: update coding-plan-cache.json
    print(c_yellow("[5/9] Updating plan cache..."))
    if not update_coding_plan_cache(args.dry_run):
        print(c_red("   Failed to update plan cache. Aborting."))
        return 1
    print()

    # Step 6: remove providerFamilyDomain from setting.json
    print(c_yellow("[6/9] Clearing provider family domain..."))
    if not clear_provider_family_domain(args.dry_run):
        print(c_red("   Failed to clear provider family domain. Aborting."))
        return 1
    print()

    # Step 7: delete telemetry file
    print(c_yellow("[7/9] Deleting telemetry-state.json..."))
    if TELEMETRY_PATH.exists():
        if args.dry_run:
            print(c_gray(f"   [dry-run] would delete {TELEMETRY_PATH}"))
        else:
            TELEMETRY_PATH.unlink()
            print(c_green("   Deleted."))
    else:
        print(c_gray("   File not found, skipping."))
    print()

    # Step 8: launch ZCode
    if args.no_launch:
        print(c_yellow("[8/9] Skipping launch (--no-launch)."))
    else:
        print(c_yellow("[8/9] Launching zcode..."))
        launched = launch_zcode(args.dry_run)
        if launched:
            print(c_gray(f"   Waiting for zcode to initialize ({LAUNCH_WAIT_SEC}s)..."))
            if not args.dry_run:
                time.sleep(LAUNCH_WAIT_SEC)
    print()

    # Step 9: verify new deviceMid
    print(c_yellow("[9/9] Checking new deviceMid..."))
    if args.no_launch:
        new_mid = "N/A (launch skipped)"
        print(c_gray("   Skipped - ZCode was not launched."))
    elif TELEMETRY_PATH.exists():
        new_mid = read_device_mid(TELEMETRY_PATH)
        print(c_green(f"   New deviceMid: {new_mid}"))
    elif args.dry_run:
        new_mid = "N/A (dry-run)"
        print(c_gray("   [dry-run] file would be regenerated by ZCode"))
    else:
        new_mid = "N/A"
        print(c_red("   telemetry-state.json not yet created."))
        print(c_yellow("   Please wait and re-run this script to verify."))

    # Result
    print()
    banner("RESULT")
    print(f"   Old deviceMid: {old_mid}")
    print(f"   New deviceMid: {new_mid}")
    print()

    if str(old_mid) == str(new_mid) and not args.dry_run:
        print(c_red("   [FAIL] deviceMid unchanged!"))
        result_code = 1
    else:
        if not args.dry_run:
            print(c_green("   [SUCCESS] Device ID changed successfully!"))
        else:
            print(c_gray("   [dry-run] no changes made."))
        result_code = 0

    print(c_cyan("=" * 40))
    return result_code


if __name__ == "__main__":
    sys.exit(main())

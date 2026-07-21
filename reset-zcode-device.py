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
    python reset-zcode-device.py            # run the full reset
    python reset-zcode-device.py --dry-run  # show what would happen, change nothing
    python reset-zcode-device.py --no-launch  # reset without relaunching ZCode
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEMETRY_PATH = Path.home() / ".zcode" / "v2" / "telemetry-state.json"
CREDENTIALS_PATH = Path.home() / ".zcode" / "v2" / "credentials.json"
CONFIG_PATH = Path.home() / ".zcode" / "v2" / "config.json"
CODING_PLAN_CACHE_PATH = Path.home() / ".zcode" / "v2" / "coding-plan-cache.json"
SETTING_PATH = Path.home() / ".zcode" / "v2" / "setting.json"

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
    args = parser.parse_args()

    banner("ZCode Device ID Reset Tool")
    if args.dry_run:
        print(c_yellow("DRY RUN - no changes will be made"))
    print()

    # Step 1: read current deviceMid
    print(c_yellow("[1/9] Reading current deviceMid..."))
    old_mid = read_device_mid(TELEMETRY_PATH)
    if TELEMETRY_PATH.exists():
        print(c_green(f"   Current deviceMid: {old_mid}"))
    else:
        print(c_gray("   telemetry-state.json not found."))
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

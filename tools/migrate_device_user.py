"""
Standalone Biometric Device-to-Device Migration CLI Tool.

Transfers an employee profile (User ID / PIN, name, privilege, RFID card, password)
and all enrolled biometric fingerprint templates directly between two ZKTeco / eSSL devices
over TCP port 4370.

ZERO DATABASE DEPENDENCY:
Runs entirely through direct TCP device socket communication.
Attendance logs and punches are intentionally NOT migrated.

Usage:
  # Interactive mode:
  python tools/migrate_device_user.py

  # Automated CLI mode:
  python tools/migrate_device_user.py --source-ip 192.168.1.209 --target-ip 192.168.1.210 --user-id 101 --mode copy

  # Simulation / Dry-run test mode (no hardware required):
  python tools/migrate_device_user.py --source-ip 192.168.1.209 --target-ip 192.168.1.210 --user-id 101 --mode copy --simulate
"""
import argparse
import os
import sys

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.device_migration import (
    fetch_device_users,
    probe_device_connection,
    run_migration,
)


# Ensure Windows terminal doesn't crash on emoji/Unicode output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_banner():
    print("\n" + "=" * 75)
    print("  [*] eSSL / ZKTeco Biometric Device-to-Device Migration Tool")
    print("  Direct TCP 4370 Employee & Fingerprint Transfer (Zero DB)")
    print("=" * 75 + "\n")


def interactive_mode():
    print_banner()

    sim_input = input("Use built-in simulated test devices? (y/N): ").strip().lower()
    simulate = sim_input in ["y", "yes"]

    default_src_sn = "NFZ8254900401"
    default_tgt_sn = "UFZ9876543210"

    src_id = input(f"Enter Source Device Serial Number [{default_src_sn}]: ").strip() or default_src_sn
    tgt_id = input(f"Enter Target Device Serial Number [{default_tgt_sn}]: ").strip() or default_tgt_sn

    print(f"\nTesting connection to Source (SN: {src_id})...")
    src_test = probe_device_connection(src_id, simulate=simulate)
    if not src_test.get("ok"):
        print(f"[ERROR] Source connection failed: {src_test.get('error')}")
        sys.exit(1)
    print(f"  [OK] Connected: {src_test.get('deviceName')} (SN: {src_test.get('serialNumber')}, IP: {src_test.get('ip')}, FP: v{src_test.get('fpVersion')})")

    print(f"Testing connection to Target (SN: {tgt_id})...")
    tgt_test = probe_device_connection(tgt_id, simulate=simulate)
    if not tgt_test.get("ok"):
        print(f"[ERROR] Target connection failed: {tgt_test.get('error')}")
        sys.exit(1)
    print(f"  [OK] Connected: {tgt_test.get('deviceName')} (SN: {tgt_test.get('serialNumber')}, IP: {tgt_test.get('ip')}, FP: v{tgt_test.get('fpVersion')})")

    # Scan and display users
    print(f"\nScanning employees on Source Device ({src_test.get('serialNumber')})...")
    users_res = fetch_device_users(src_id, simulate=simulate)
    if not users_res.get("ok"):
        print(f"[ERROR] Could not read users: {users_res.get('error')}")
        sys.exit(1)

    users = users_res.get("users", [])
    print("\n" + "-" * 75)
    print(f"{'User ID':<10} | {'Name':<22} | {'Role':<14} | {'Enrolled Fingers'}")
    print("-" * 75)
    for u in users:
        f_desc = f"{u['fingerCount']} Finger(s) (FIDs: {u['fingerFids']})" if u['fingerCount'] > 0 else "None (Card/PIN only)"
        print(f"{u['userId']:<10} | {u['name']:<22} | {u['roleLabel']:<14} | {f_desc}")
    print("-" * 75 + "\n")

    user_id = input("Enter Employee User ID to migrate (or 'ALL'): ").strip()
    if not user_id:
        print("[CANCELLED] No user ID entered.")
        sys.exit(0)

    mode = input("Select Transfer Mode ('copy' or 'move') [copy]: ").strip().lower() or "copy"
    if mode not in ["copy", "move"]:
        mode = "copy"

    target_uids = [u["userId"] for u in users] if user_id.upper() == "ALL" else [user_id]

    print(f"\nProceeding to {mode.upper()} {len(target_uids)} employee(s)...")
    results = run_migration(
        source_ip=src_id,
        source_port=src_test.get("port", 4370),
        target_ip=tgt_id,
        target_port=tgt_test.get("port", 4370),
        user_ids=target_uids,
        mode=mode,
        simulate=simulate
    )

    print("\n" + "=" * 75)
    print("  MIGRATION EXECUTION LOGS")
    print("=" * 75)
    for r in results:
        print(f"\n>>> Employee: {r.get('userName')} (ID: {r.get('userId')})")
        for l in r.get("logs", []):
            print(f"    {l}")
        if r.get("ok"):
            print(f"  [SUCCESS] Migrated with {r.get('fingersTransferred')} enrolled fingerprint(s).")
        else:
            print(f"  [FAILED] Error: {r.get('error')}")
    print("\n" + "=" * 75 + "\n")


def main():
    parser = argparse.ArgumentParser(description="eSSL / ZKTeco Biometric Device Migration Tool")
    parser.add_argument("--source-sn", help="Source Device Serial Number (e.g. NFZ8254900401)")
    parser.add_argument("--source-ip", help="Source Device IP address (optional if SN is registered)")
    parser.add_argument("--source-port", type=int, default=4370, help="Source Device Port (default: 4370)")
    parser.add_argument("--target-sn", help="Target Device Serial Number (e.g. UFZ9876543210)")
    parser.add_argument("--target-ip", help="Target Device IP address (optional if SN is registered)")
    parser.add_argument("--target-port", type=int, default=4370, help="Target Device Port (default: 4370)")
    parser.add_argument("--user-id", help="Employee User ID / PIN to migrate, or 'ALL'")
    parser.add_argument("--mode", choices=["copy", "move"], default="copy", help="Transfer mode: copy (default) or move")
    parser.add_argument("--simulate", action="store_true", help="Run with simulated devices for testing without hardware")

    args = parser.parse_args()

    src_id = args.source_sn or args.source_ip
    tgt_id = args.target_sn or args.target_ip

    # If no source/target specified, drop into interactive mode
    if not src_id or not tgt_id:
        interactive_mode()
        return

    print_banner()
    results = run_migration(
        source_ip=src_id,
        source_port=args.source_port,
        target_ip=tgt_id,
        target_port=args.target_port,
        user_ids=[args.user_id] if args.user_id else ["101"],
        mode=args.mode,
        simulate=args.simulate
    )

    for r in results:
        print(f">>> User {r['userId']} ({r['userName']}) -> {args.mode.upper()}: {'SUCCESS' if r['ok'] else 'FAILED'}")
        for l in r["logs"]:
            print(f"    {l}")

    all_ok = all(r["ok"] for r in results)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

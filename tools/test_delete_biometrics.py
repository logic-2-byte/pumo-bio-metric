"""
Comprehensive Biometric & Copied User Deletion Test CLI Tool.

Tests deleting copied users, removing biometrics from target/source readers,
and offboarding / firing employees with biometric wiping across all devices.

Usage:
  # 1. Run full simulated test suite (zero hardware required):
  python tools/test_delete_biometrics.py --test-simulated

  # 2. Run API integration tests against running server (http://localhost:8000):
  python tools/test_delete_biometrics.py --test-api

  # 3. Test deletion against real hardware device over TCP 4370:
  python tools/test_delete_biometrics.py --real --device 192.168.0.154 --user 101
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def green(text: str) -> str:
    return color(text, "32")

def red(text: str) -> str:
    return color(text, "31")

def yellow(text: str) -> str:
    return color(text, "33")

def cyan(text: str) -> str:
    return color(text, "36")

def bold(text: str) -> str:
    return color(text, "1")

def run_simulated_tests():
    from app.core.device_migration import (
        SIMULATED_DEVICES,
        delete_batch_device_users,
        delete_device_user,
        fetch_device_users,
        reset_simulation_state,
        run_migration,
    )
    from app.core.employee_service import (
        fire_employee_and_wipe_biometrics,
        load_employees,
        revoke_employee_device_access,
    )

    print("=" * 75)
    print(bold(cyan("  [+] RUNNING BIOMETRIC DELETION TEST SUITE (SIMULATED MODE) ")))
    print("=" * 75)

    reset_simulation_state()
    src_sn = "NFZ8254900401"
    tgt_sn = "UFZ9876543210"

    # TEST 1: Copy User to Target, then Delete Copied User from Target Only
    print(bold("\n[TEST 1] Copying User 101 to Target (" + tgt_sn + ") & Deleting Copied User from Target..."))
    mig_res = run_migration(
        source_ip=src_sn,
        target_ip=tgt_sn,
        user_ids=["101"],
        mode="copy",
        simulate=True
    )
    assert all(r.get("ok") for r in mig_res), f"Migration failed: {mig_res}"

    tgt_users_before = fetch_device_users(tgt_sn, simulate=True).get("users", [])
    assert any(str(u["userId"]) == "101" for u in tgt_users_before), "User 101 should exist on Target after copy"
    print("  " + green("[OK]") + " User 101 successfully copied to Target reader.")

    # Now delete the copied user from Target reader
    del_res = delete_device_user(device_id=tgt_sn, user_id="101", simulate=True)
    assert del_res.get("ok") and del_res.get("deleted"), f"Delete on target failed: {del_res}"

    tgt_users_after = fetch_device_users(tgt_sn, simulate=True).get("users", [])
    src_users = fetch_device_users(src_sn, simulate=True).get("users", [])

    assert not any(str(u["userId"]) == "101" for u in tgt_users_after), "User 101 must be gone from Target"
    assert any(str(u["userId"]) == "101" for u in src_users), "User 101 MUST remain on Source reader"
    print("  " + green("[OK]") + " PASS: Copied User 101 deleted from Target device only. Source reader remains intact!")

    # TEST 2: Clear Fingerprints Only vs Delete User
    print(bold("\n[TEST 2] Testing 'Fingerprints Only' Deletion..."))
    run_migration(source_ip=src_sn, target_ip=tgt_sn, user_ids=["101"], mode="copy", simulate=True)
    fp_del_res = delete_device_user(device_id=tgt_sn, user_id="101", delete_biometrics_only=True, simulate=True)
    assert fp_del_res.get("ok") and fp_del_res.get("deleted") and fp_del_res.get("biometricsOnly")

    tgt_users_fp = fetch_device_users(tgt_sn, simulate=True).get("users", [])
    u101 = next((u for u in tgt_users_fp if str(u["userId"]) == "101"), None)
    assert u101 is not None, "User should still exist on device"
    assert u101["fingerCount"] == 0, "Fingerprint count should be 0"
    print("  " + green("[OK]") + " PASS: Biometric fingerprints wiped (count=0), but user profile retained!")

    # TEST 3: Offboarding / Firing Employee — Wipe Across ALL Enrolled Readers
    print(bold("\n[TEST 3] Testing Fire Employee & Wipe Biometrics Across All Readers..."))
    delete_device_user(device_id=tgt_sn, user_id="101", simulate=True)
    run_migration(source_ip=src_sn, target_ip=tgt_sn, user_ids=["101"], mode="copy", simulate=True)

    fire_res = fire_employee_and_wipe_biometrics(
        employee_id=101,
        action="fire_all",
        delete_mode="full",
        simulate=True
    )
    assert fire_res.get("ok"), f"Fire employee failed: {fire_res}"
    print(f"  Devices wiped: {fire_res.get('biometricsWipedCount')} / {fire_res.get('devicesContacted')}")

    src_after_fire = fetch_device_users(src_sn, simulate=True).get("users", [])
    assert not any(str(u["userId"]) == "101" for u in src_after_fire), "User 101 should be wiped from Source"

    emp_record = fire_res.get("employee", {})
    assert emp_record.get("status") == "Terminated", f"Expected Terminated, got {emp_record.get('status')}"
    assert len(emp_record.get("access", [])) == 0, "All device access entries must be cleared"
    print("  " + green("[OK]") + " PASS: Employee 101 status set to 'Terminated' and biometrics wiped from all readers!")

    # TEST 4: Batch Deletion of Multiple Users
    print(bold("\n[TEST 4] Testing Batch Deletion of Multiple Users..."))
    batch_res = delete_batch_device_users(
        device_id=src_sn,
        user_ids=["102", "111"],
        simulate=True
    )
    assert batch_res.get("ok"), f"Batch delete failed: {batch_res}"
    assert batch_res.get("deletedCount") >= 1, "Should have deleted at least 1 user"
    print(f"  " + green("[OK]") + f" PASS: Batch delete successfully removed {batch_res.get('deletedCount')} users in one operation.")

    print("=" * 75)
    print(bold(green("  🎉 ALL SIMULATED BIOMETRIC DELETION TESTS PASSED SUCCESSFULLY!")))
    print("=" * 75)
    return True

def run_api_tests(base_url: str = "http://localhost:8000"):
    print("=" * 75)
    print(bold(cyan(f"  🌐 RUNNING API INTEGRATION TESTS AGAINST {base_url}")))
    print("=" * 75)

    def req(path: str, data: dict | None = None, method: str = "GET") -> dict:
        url = f"{base_url}{path}"
        headers = {"Content-Type": "application/json"} if data else {}
        body = json.dumps(data).encode("utf-8") if data else None
        r = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        devs = req("/api/migration/devices")
        print("  " + green("[OK]") + f" Server is responsive on {base_url}. Found {len(devs.get('devices', []))} devices.")
    except Exception as e:
        print("  " + red("[ERR]") + f" Cannot connect to {base_url}: {e}")
        print("     Please ensure the biometric server is running (e.g. uvicorn app.main:app --port 8000).")
        return False

    req("/api/migration/reset-sim", data={}, method="POST")

    print(bold("\n[API TEST 1] POST /api/migration/transfer-user (Copying 101 to Target)..."))
    copy_res = req("/api/migration/transfer-user", data={
        "sourceSn": "NFZ8254900401",
        "targetSn": "UFZ9876543210",
        "userId": "101",
        "mode": "copy",
        "simulate": True
    }, method="POST")
    assert copy_res.get("ok"), f"Copy failed: {copy_res}"
    print("  " + green("[OK]") + " Copied user 101 via API.")

    print(bold("\n[API TEST 2] POST /api/migration/delete (Deleting Copied User from Target)..."))
    del_res = req("/api/migration/delete", data={
        "sn": "UFZ9876543210",
        "userId": "101",
        "simulate": True
    }, method="POST")
    assert del_res.get("ok") and del_res.get("deleted"), f"Delete on target failed: {del_res}"
    print("  " + green("[OK]") + " PASS: POST /api/migration/delete successfully removed copied user from Target device!")

    print(bold("\n[API TEST 3] POST /api/migration/delete-batch (Batch deleting users)..."))
    batch_res = req("/api/migration/delete-batch", data={
        "sn": "NFZ8254900401",
        "userIds": ["102", "111"],
        "simulate": True
    }, method="POST")
    assert batch_res.get("ok"), f"Batch delete API failed: {batch_res}"
    print("  " + green("[OK]") + f" PASS: POST /api/migration/delete-batch deleted {batch_res.get('deletedCount')} users!")

    print(bold("\n[API TEST 4] POST /api/employees/offboard (Firing employee and wiping biometrics)..."))
    offboard_res = req("/api/employees/offboard", data={
        "employeeId": 101,
        "action": "fire_all",
        "deleteMode": "full",
        "simulate": True
    }, method="POST")
    assert offboard_res.get("ok"), f"Offboard API failed: {offboard_res}"
    print("  " + green("[OK]") + f" PASS: POST /api/employees/offboard successfully executed! {offboard_res.get('message')}")

    print(bold("\n[API TEST 5] POST /api/employees/revoke-access (Revoking single reader access)..."))
    revoke_res = req("/api/employees/revoke-access", data={
        "employeeId": 102,
        "deviceSn": "NFZ8254900401",
        "simulate": True
    }, method="POST")
    assert revoke_res.get("ok"), f"Revoke access API failed: {revoke_res}"
    print("  " + green("[OK]") + " PASS: POST /api/employees/revoke-access executed successfully!")

    print("=" * 75)
    print(bold(green(f"  🎉 ALL HTTP API DELETION ENDPOINTS VERIFIED AND WORKING ON {base_url}!")))
    print("=" * 75)
    return True

def run_real_device_delete(device_ip: str, user_id: str, port: int = 4370, biometrics_only: bool = False):
    from app.core.device_migration import delete_device_user, probe_device_connection

    print("=" * 75)
    print(bold(yellow(f"  REAL HARDWARE BIOMETRIC DELETION: {device_ip}:{port} (User: {user_id})")))
    print("=" * 75)

    print(f"Probing hardware reader at {device_ip}:{port}...")
    probe = probe_device_connection(device_ip, port=port, simulate=False)
    if not probe.get("ok"):
        print("  " + red("[ERR]") + f" Connection failed: {probe.get('error')}")
        return False

    print("  " + green("[OK]") + f" Connected to: {probe.get('deviceName')} (SN: {probe.get('serialNumber')}, IP: {probe.get('ip')})")
    scope = "FINGERPRINTS ONLY" if biometrics_only else "FULL USER & FINGERPRINTS"
    confirm = input(f"Are you sure you want to delete {scope} for User {user_id} on real device {device_ip}? (y/N): ").strip().lower()
    if confirm not in ["y", "yes"]:
        print("Aborted by user.")
        return False

    print(f"Executing deletion on real device {device_ip}...")
    res = delete_device_user(
        device_id=device_ip,
        user_id=user_id,
        port=port,
        delete_biometrics_only=biometrics_only,
        simulate=False
    )
    if res.get("ok") and res.get("deleted"):
        print("  " + green("[OK]") + f" SUCCESS: {res.get('message')}")
        return True
    else:
        print("  " + yellow("!") + f" Result: {res.get('message') or res.get('error')}")
        return res.get("ok", False)

def main():
    parser = argparse.ArgumentParser(description="Biometric & Copied User Deletion Test Tool")
    parser.add_argument("--test-simulated", action="store_true", help="Run simulated test suite (zero hardware required)")
    parser.add_argument("--test-api", action="store_true", help="Run HTTP API tests against http://localhost:8000")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL for API testing (default: http://localhost:8000)")
    parser.add_argument("--real", action="store_true", help="Execute against a real physical reader")
    parser.add_argument("--device", help="Device IP or Serial Number for real test")
    parser.add_argument("--user", help="User ID / PIN to delete")
    parser.add_argument("--port", type=int, default=4370, help="Device TCP port (default: 4370)")
    parser.add_argument("--biometrics-only", action="store_true", help="Delete fingerprint templates only, keep user PIN")

    args = parser.parse_args()

    if args.real:
        if not args.device or not args.user:
            print("[ERROR] When using --real, both --device and --user are required.")
            sys.exit(1)
        ok = run_real_device_delete(args.device, args.user, port=args.port, biometrics_only=args.biometrics_only)
        sys.exit(0 if ok else 1)

    if args.test_api:
        ok = run_api_tests(args.url)
        sys.exit(0 if ok else 1)

    if args.test_simulated or len(sys.argv) == 1:
        ok = run_simulated_tests()
        sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
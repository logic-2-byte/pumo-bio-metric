"""
Employee & Biometric Branch Access Service.

Provides employee directory management, branch transfer orchestration,
multi-branch roaming access (Copy), and physical reader deletion (Revoke).
Seamlessly bridges PostgreSQL (l2b_lms) if available, with graceful
in-memory / local JSON fallback.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any

from app.core.device_migration import (
    delete_device_user,
    get_registered_devices,
    migrate_single_user_real,
    migrate_single_user_simulated,
    resolve_target_pin,
)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
EMPLOYEE_FILE = os.path.join(BASE_DIR, "data", "employees.json")
USER_NAMES_FILE = os.path.join(BASE_DIR, "user_names.json")

DEFAULT_BRANCHES = [
    {
        "id": 1,
        "code": "BR-01",
        "name": "Main Campus (HQ)",
        "devices": ["NFZ8254900401"]
    },
    {
        "id": 2,
        "code": "BR-02",
        "name": "Floor 2 / City Branch",
        "devices": ["UFZ9876543210"]
    },
    {
        "id": 3,
        "code": "BR-03",
        "name": "Annex / Workshop",
        "devices": ["NFZ8242802542"]
    },
    {
        "id": 4,
        "code": "BR-04",
        "name": "Regional Center",
        "devices": ["ZK1"]
    },
]

# Initial bootstrap employees if data/employees.json doesn't exist yet
DEFAULT_EMPLOYEES = [
    {
        "id": 101,
        "employeeCode": "EMP-101",
        "name": "Member 1o",
        "role": "Normal User",
        "branchId": 1,
        "department": "Engineering",
        "access": [
            {"deviceSn": "NFZ8254900401", "pin": "101", "primary": True}
        ]
    },
    {
        "id": 102,
        "employeeCode": "EMP-102",
        "name": "Gowtham",
        "role": "Normal User",
        "branchId": 1,
        "department": "Operations",
        "access": [
            {"deviceSn": "NFZ8254900401", "pin": "102", "primary": True}
        ]
    },
    {
        "id": 111,
        "employeeCode": "EMP-111",
        "name": "ARULAJAY",
        "role": "Super Admin",
        "branchId": 1,
        "department": "Management",
        "access": [
            {"deviceSn": "NFZ8254900401", "pin": "111", "primary": True}
        ]
    },
    {
        "id": 9999,
        "employeeCode": "EMP-9999",
        "name": "Master Super Admin",
        "role": "Super Admin",
        "branchId": 2,
        "department": "Security",
        "access": [
            {"deviceSn": "UFZ9876543210", "pin": "9999", "primary": True}
        ]
    }
]


def load_employees() -> list[dict[str, Any]]:
    """Load employees from persistent file or initialize defaults."""
    os.makedirs(os.path.dirname(EMPLOYEE_FILE), exist_ok=True)
    if os.path.exists(EMPLOYEE_FILE):
        try:
            with open(EMPLOYEE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not read employees.json: %s", e)

    # Save default employees
    save_employees(DEFAULT_EMPLOYEES)
    return list(DEFAULT_EMPLOYEES)


def save_employees(employees: list[dict[str, Any]]) -> None:
    """Save employees to persistent file."""
    os.makedirs(os.path.dirname(EMPLOYEE_FILE), exist_ok=True)
    try:
        with open(EMPLOYEE_FILE, "w", encoding="utf-8") as f:
            json.dump(employees, f, indent=2)
    except Exception as e:
        logger.error("Failed to save employees: %s", e)


def get_branches() -> list[dict[str, Any]]:
    """Return all branches and their associated biometric readers."""
    registered = get_registered_devices()
    branches = []
    for b in DEFAULT_BRANCHES:
        dev_list = []
        for sn in b.get("devices", []):
            info = registered.get(sn, {"sn": sn, "name": f"Reader ({sn})", "ip": ""})
            dev_list.append({
                "sn": sn,
                "name": info.get("name", sn),
                "ip": info.get("ip", ""),
                "port": info.get("port", 4370)
            })
        branches.append({
            "id": b["id"],
            "code": b["code"],
            "name": b["name"],
            "devices": dev_list
        })
    return branches


def get_employee_by_id(emp_id: str | int) -> dict[str, Any] | None:
    """Find employee by ID or employee code."""
    target = str(emp_id).strip()
    for e in load_employees():
        if str(e["id"]) == target or str(e.get("employeeCode", "")).lower() == target.lower():
            return e
    return None


def transfer_employee(
    employee_id: str | int,
    to_branch_id: int,
    effective_date: str | None = None,
    reason: str | None = None,
    transfer_biometric: bool = True,
    biometric_mode: str = "move",
    simulate: bool = False,
    timeout: int = 8
) -> dict[str, Any]:
    """
    Execute an employee branch transfer with integrated biometric migration,
    copy (multi-branch access), or PIN collision handling.
    """
    employees = load_employees()
    target_id = str(employee_id).strip()
    emp = next((e for e in employees if str(e["id"]) == target_id or str(e.get("employeeCode", "")).lower() == target_id.lower()), None)
    if not emp:
        return {"ok": False, "error": f"Employee {employee_id} not found."}

    from_branch_id = emp["branchId"]
    if from_branch_id == to_branch_id:
        return {"ok": False, "error": "Destination branch must be different from current branch."}

    branches = {b["id"]: b for b in get_branches()}
    from_branch = branches.get(from_branch_id)
    to_branch = branches.get(to_branch_id)

    if not to_branch:
        return {"ok": False, "error": f"Destination branch ID {to_branch_id} does not exist."}

    effective = effective_date or date.today().isoformat()
    mode = biometric_mode.lower().strip()
    if mode not in ("move", "copy"):
        mode = "move"

    biometric_result: dict[str, Any] | None = None

    if transfer_biometric:
        src_devices = from_branch.get("devices", []) if from_branch else []
        tgt_devices = to_branch.get("devices", [])

        if not src_devices:
            biometric_result = {
                "ok": True,
                "skipped": True,
                "reason": f"Current branch ({from_branch['name'] if from_branch else from_branch_id}) has no registered biometric readers."
            }
        elif not tgt_devices:
            biometric_result = {
                "ok": True,
                "skipped": True,
                "reason": f"Target branch ({to_branch['name']}) has no registered biometric readers."
            }
        else:
            # Primary devices for source and target
            src_dev = src_devices[0]
            tgt_dev = tgt_devices[0]
            src_sn = src_dev["sn"]
            tgt_sn = tgt_dev["sn"]

            # Find employee's PIN on source device
            existing_access = next((a for a in emp.get("access", []) if a.get("deviceSn") == src_sn), None)
            emp_pin = existing_access["pin"] if existing_access else str(emp["id"])

            # Check target PIN collision / mapping
            pin_check = resolve_target_pin(tgt_sn, emp_pin, user_name=emp["name"], simulate=simulate)
            target_pin = pin_check["targetPin"]

            logger.info("Executing %s biometric migration: User %s (Source: %s PIN %s -> Target: %s PIN %s)",
                        mode.upper(), emp["name"], src_sn, emp_pin, tgt_sn, target_pin)

            if simulate:
                res = migrate_single_user_simulated(
                    source_id=src_sn,
                    target_id=tgt_sn,
                    user_id=emp_pin,
                    mode=mode,
                    target_user_id=target_pin
                )
            else:
                res = migrate_single_user_real(
                    source_id=src_sn,
                    source_port=src_dev.get("port", 4370),
                    target_id=tgt_sn,
                    target_port=tgt_dev.get("port", 4370),
                    user_id=emp_pin,
                    mode=mode,
                    timeout=timeout,
                    target_user_id=target_pin
                )

            biometric_result = res.to_dict()
            biometric_result["sourceDevice"] = src_sn
            biometric_result["targetDevice"] = tgt_sn
            biometric_result["isPinMapped"] = pin_check.get("isMapped", False)
            biometric_result["targetPin"] = target_pin

            # Update employee's device access records
            if res.ok:
                access_list = list(emp.get("access", []))
                if mode == "move":
                    # Remove source device access
                    access_list = [a for a in access_list if a.get("deviceSn") != src_sn]
                    # Add/update target device access
                    tgt_access = next((a for a in access_list if a.get("deviceSn") == tgt_sn), None)
                    if tgt_access:
                        tgt_access["pin"] = target_pin
                        tgt_access["primary"] = True
                    else:
                        access_list.append({"deviceSn": tgt_sn, "pin": target_pin, "primary": True})
                elif mode == "copy":
                    # Keep source device, add target device access (Multi-Branch Access!)
                    tgt_access = next((a for a in access_list if a.get("deviceSn") == tgt_sn), None)
                    if tgt_access:
                        tgt_access["pin"] = target_pin
                    else:
                        access_list.append({"deviceSn": tgt_sn, "pin": target_pin, "primary": False})
                emp["access"] = access_list

    # Update employee branch
    emp["branchId"] = to_branch_id
    emp.setdefault("transferHistory", []).append({
        "fromBranchId": from_branch_id,
        "fromBranchName": from_branch["name"] if from_branch else f"Branch {from_branch_id}",
        "toBranchId": to_branch_id,
        "toBranchName": to_branch["name"],
        "effectiveDate": effective,
        "reason": reason or "Transferred to new campus",
        "biometricTransferred": transfer_biometric,
        "biometricMode": mode if transfer_biometric else None,
        "biometricOk": biometric_result.get("ok", False) if biometric_result else False
    })

    save_employees(employees)

    return {
        "ok": True,
        "employee": emp,
        "fromBranch": from_branch,
        "toBranch": to_branch,
        "effectiveDate": effective,
        "biometric": biometric_result,
        "message": f"Successfully transferred {emp['name']} to {to_branch['name']}."
    }


def grant_multi_branch_access(
    employee_id: str | int,
    target_branch_id: int | None = None,
    target_device_sn: str | None = None,
    simulate: bool = False,
    timeout: int = 8
) -> dict[str, Any]:
    """
    Grant an employee biometric access to another branch reader (Copy Mode)
    without transferring their home branch. Enables multi-branch punch roaming.
    """
    employees = load_employees()
    target_id = str(employee_id).strip()
    emp = next((e for e in employees if str(e["id"]) == target_id or str(e.get("employeeCode", "")).lower() == target_id.lower()), None)
    if not emp:
        return {"ok": False, "error": f"Employee {employee_id} not found."}

    branches = {b["id"]: b for b in get_branches()}
    home_branch = branches.get(emp["branchId"])
    if not home_branch or not home_branch.get("devices"):
        return {"ok": False, "error": f"Employee's home branch ({emp['branchId']}) has no reader to copy biometrics from."}

    src_dev = home_branch["devices"][0]
    src_sn = src_dev["sn"]

    tgt_sn = target_device_sn
    tgt_dev = None
    if not tgt_sn and target_branch_id:
        tgt_b = branches.get(target_branch_id)
        if tgt_b and tgt_b.get("devices"):
            tgt_dev = tgt_b["devices"][0]
            tgt_sn = tgt_dev["sn"]

    if not tgt_sn:
        return {"ok": False, "error": "Target branch or device Serial Number is required."}

    if not tgt_dev:
        registered = get_registered_devices()
        tgt_dev = registered.get(tgt_sn, {"sn": tgt_sn, "ip": "", "port": 4370})

    existing_access = next((a for a in emp.get("access", []) if a.get("deviceSn") == src_sn), None)
    emp_pin = existing_access["pin"] if existing_access else str(emp["id"])

    # Resolve PIN mapping on target reader
    pin_check = resolve_target_pin(tgt_sn, emp_pin, user_name=emp["name"], simulate=simulate)
    target_pin = pin_check["targetPin"]

    logger.info("Granting multi-branch access: Copying %s from %s to %s (Target PIN: %s)",
                emp["name"], src_sn, tgt_sn, target_pin)

    if simulate:
        res = migrate_single_user_simulated(
            source_id=src_sn,
            target_id=tgt_sn,
            user_id=emp_pin,
            mode="copy",
            target_user_id=target_pin
        )
    else:
        res = migrate_single_user_real(
            source_id=src_sn,
            source_port=src_dev.get("port", 4370),
            target_id=tgt_sn,
            target_port=tgt_dev.get("port", 4370),
            user_id=emp_pin,
            mode="copy",
            timeout=timeout,
            target_user_id=target_pin
        )

    if res.ok:
        access_list = list(emp.get("access", []))
        found_tgt = next((a for a in access_list if a.get("deviceSn") == tgt_sn), None)
        if found_tgt:
            found_tgt["pin"] = target_pin
        else:
            access_list.append({"deviceSn": tgt_sn, "pin": target_pin, "primary": False})
        emp["access"] = access_list
        save_employees(employees)

    out = res.to_dict()
    out["isPinMapped"] = pin_check.get("isMapped", False)
    out["targetPin"] = target_pin
    return {
        "ok": res.ok,
        "employee": emp,
        "biometric": out,
        "message": f"Multi-branch access to {tgt_sn} {'granted successfully' if res.ok else 'failed'}."
    }


def revoke_employee_device_access(
    employee_id: str | int,
    device_sn: str,
    delete_biometrics_only: bool = False,
    simulate: bool = False,
    timeout: int = 5
) -> dict[str, Any]:
    """
    Revoke biometric access by deleting the employee's fingerprints & user profile
    (or biometric templates only) from a specific physical reader device (or mock device).
    """
    employees = load_employees()
    target_id = str(employee_id).strip()
    emp = next((e for e in employees if str(e["id"]) == target_id or str(e.get("employeeCode", "")).lower() == target_id.lower()), None)
    if not emp:
        return {"ok": False, "error": f"Employee {employee_id} not found."}

    access_entry = next((a for a in emp.get("access", []) if a.get("deviceSn") == device_sn), None)
    pin = access_entry["pin"] if access_entry else str(emp["id"])

    logger.info("Revoking biometric access: Deleting user %s (PIN: %s) from device %s (biometrics_only=%s)",
                emp["name"], pin, device_sn, delete_biometrics_only)

    registered = get_registered_devices()
    dev_info = registered.get(device_sn, {"sn": device_sn, "port": 4370})
    port = dev_info.get("port", 4370)

    del_res = delete_device_user(
        device_id=device_sn,
        user_id=pin,
        port=port,
        timeout=timeout,
        delete_biometrics_only=delete_biometrics_only,
        simulate=simulate
    )

    if del_res.get("ok"):
        if not delete_biometrics_only:
            # Remove from employee's access list completely
            emp["access"] = [a for a in emp.get("access", []) if a.get("deviceSn") != device_sn]
        else:
            # Keep access entry but mark biometrics cleared
            for a in emp.get("access", []):
                if a.get("deviceSn") == device_sn:
                    a["biometricsCleared"] = True
        save_employees(employees)

    return {
        "ok": del_res.get("ok", False),
        "employee": emp,
        "deleted": del_res.get("deleted", False),
        "biometricsOnly": delete_biometrics_only,
        "message": del_res.get("message", f"Revoked access for {emp['name']} on {device_sn}.")
    }


def fire_employee_and_wipe_biometrics(
    employee_id: str | int,
    action: str = "fire_all",       # "fire_all" or "delete_device"
    device_sn: str | None = None,
    delete_mode: str = "full",      # "full" or "biometrics_only"
    simulate: bool = False,
    timeout: int = 5
) -> dict[str, Any]:
    """
    Dedicated Offboarding & Biometric Wiping:
    Permanently revokes and deletes an employee's biometric fingerprints (and user profile)
    across ALL enrolled hardware readers (when fired/dismissed) or from a specific reader (e.g. copied/roaming).
    """
    employees = load_employees()
    target_id = str(employee_id).strip()
    emp = next((e for e in employees if str(e["id"]) == target_id or str(e.get("employeeCode", "")).lower() == target_id.lower()), None)
    if not emp:
        return {"ok": False, "error": f"Employee {employee_id} not found."}

    delete_biometrics_only = (delete_mode == "biometrics_only")
    registered = get_registered_devices()

    # Determine devices to target
    target_devices: list[tuple[str, str]] = []  # list of (device_sn, pin)
    access_list = emp.get("access", [])

    if action == "delete_device" and device_sn:
        # Single device deletion (e.g., removing copied reader)
        entry = next((a for a in access_list if a.get("deviceSn") == device_sn), None)
        pin = str(entry["pin"]) if entry else str(emp["id"])
        target_devices.append((device_sn, pin))
    else:
        # Fire / Terminate: Wipe from ALL enrolled readers
        if access_list:
            for a in access_list:
                target_devices.append((a["deviceSn"], str(a.get("pin", emp["id"]))))
        else:
            # If no explicit access list, target all registered devices using employee id as PIN
            for sn in registered.keys():
                target_devices.append((sn, str(emp["id"])))

    logger.info("Offboarding employee %s (ID: %s): action=%s, targets=%s, mode=%s",
                emp["name"], emp["id"], action, target_devices, delete_mode)

    device_results = []
    for sn, pin in target_devices:
        dev_info = registered.get(sn, {"sn": sn, "port": 4370})
        port = dev_info.get("port", 4370)
        res = delete_device_user(
            device_id=sn,
            user_id=pin,
            port=port,
            timeout=timeout,
            delete_biometrics_only=delete_biometrics_only,
            simulate=simulate
        )
        device_results.append({
            "deviceSn": sn,
            "pin": pin,
            "ok": res.get("ok", False),
            "deleted": res.get("deleted", False),
            "message": res.get("message", ""),
            "simulated": res.get("simulated", False)
        })

    # Update employee profile
    today_str = date.today().isoformat()
    if action == "fire_all":
        emp["status"] = "Terminated"
        emp["terminatedDate"] = today_str
        emp["access"] = []
    else:
        # Remove only targeted device from access list
        emp["access"] = [a for a in access_list if a.get("deviceSn") != device_sn]

    if "offboardHistory" not in emp:
        emp["offboardHistory"] = []
    emp["offboardHistory"].append({
        "timestamp": today_str,
        "action": action,
        "deleteMode": delete_mode,
        "deviceResults": device_results
    })

    save_employees(employees)

    wiped_count = sum(1 for r in device_results if r.get("deleted"))
    return {
        "ok": True,
        "employee": emp,
        "action": action,
        "deleteMode": delete_mode,
        "devicesContacted": len(device_results),
        "biometricsWipedCount": wiped_count,
        "deviceResults": device_results,
        "message": f"Offboarding completed for {emp['name']}: {wiped_count} device reader(s) updated."
    }


def delete_employee_permanently(
    employee_id: str | int,
    wipe_biometrics: bool = True,
    simulate: bool = False,
    timeout: int = 5
) -> dict[str, Any]:
    """
    Completely delete employee record from directory and permanently wipe biometrics from all readers.
    """
    employees = load_employees()
    target_id = str(employee_id).strip()
    emp = next((e for e in employees if str(e["id"]) == target_id or str(e.get("employeeCode", "")).lower() == target_id.lower()), None)
    if not emp:
        return {"ok": False, "error": f"Employee {employee_id} not found."}

    device_results = []
    if wipe_biometrics:
        fire_res = fire_employee_and_wipe_biometrics(
            employee_id=employee_id,
            action="fire_all",
            delete_mode="full",
            simulate=simulate,
            timeout=timeout
        )
        device_results = fire_res.get("deviceResults", [])

    # Remove employee from directory
    updated_list = [e for e in employees if str(e["id"]) != target_id and str(e.get("employeeCode", "")).lower() != target_id.lower()]
    save_employees(updated_list)

    return {
        "ok": True,
        "deleted": True,
        "employeeId": target_id,
        "employeeName": emp["name"],
        "deviceResults": device_results,
        "message": f"Employee {emp['name']} permanently removed and biometrics wiped from all readers."
    }

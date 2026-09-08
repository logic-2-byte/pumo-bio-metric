"""
Unit & Integration Tests for Employee Branch Transfer, Multi-Branch Copy,
and Biometric Deletion.
"""
from fastapi.testclient import TestClient

from app.core.device_migration import (
    SIMULATED_DEVICES,
    reset_simulation_state,
)
from app.core.employee_service import (
    DEFAULT_EMPLOYEES,
    get_branches,
    get_employee_by_id,
    grant_multi_branch_access,
    load_employees,
    revoke_employee_device_access,
    save_employees,
    transfer_employee,
)
from app.main import app

client = TestClient(app)


def setup_function():
    """Reset simulated devices and employee records before each test."""
    reset_simulation_state()
    # Reset employee test fixtures
    save_employees(list(DEFAULT_EMPLOYEES))


def test_list_branches_and_employees():
    branches = get_branches()
    assert len(branches) >= 4
    b1 = next(b for b in branches if b["id"] == 1)
    assert b1["code"] == "BR-01"
    assert len(b1["devices"]) > 0

    emps = load_employees()
    assert len(emps) >= 3
    assert any(e["id"] == 101 for e in emps)


def test_employee_branch_transfer_move_mode():
    """Move mode transfers employee to Branch 2, migrates biometrics, and removes from Branch 1."""
    res = transfer_employee(
        employee_id=101,
        to_branch_id=2,
        transfer_biometric=True,
        biometric_mode="move",
        simulate=True
    )
    assert res["ok"] is True
    emp = res["employee"]
    assert emp["branchId"] == 2
    assert len(emp.get("transferHistory", [])) == 1
    assert emp["transferHistory"][0]["biometricMode"] == "move"

    bio = res["biometric"]
    assert bio is not None
    assert bio["ok"] is True
    assert bio["mode"] == "move"
    assert bio["fingersTransferred"] == 2

    # Verify target device UFZ9876543210 has user 101
    tgt_users = [u["user_id"] for u in SIMULATED_DEVICES["UFZ9876543210"]["users"]]
    assert "101" in tgt_users

    # Verify source device NFZ8254900401 NO LONGER has user 101 (Move deletes source)
    src_users = [u["user_id"] for u in SIMULATED_DEVICES["NFZ8254900401"]["users"]]
    assert "101" not in src_users


def test_employee_branch_transfer_copy_mode_multi_branch():
    """Copy mode transfers employee to Branch 2, copies biometrics, and RETAINS access on Branch 1."""
    res = transfer_employee(
        employee_id=102,
        to_branch_id=2,
        transfer_biometric=True,
        biometric_mode="copy",
        simulate=True
    )
    assert res["ok"] is True
    emp = res["employee"]
    assert emp["branchId"] == 2

    bio = res["biometric"]
    assert bio["ok"] is True
    assert bio["mode"] == "copy"
    assert bio["fingersTransferred"] == 1

    # Verify target device UFZ9876543210 has user 102
    tgt_users = [u["user_id"] for u in SIMULATED_DEVICES["UFZ9876543210"]["users"]]
    assert "102" in tgt_users

    # Verify source device NFZ8254900401 STILL HAS user 102 (Copy keeps source)
    src_users = [u["user_id"] for u in SIMULATED_DEVICES["NFZ8254900401"]["users"]]
    assert "102" in src_users


def test_grant_multi_branch_access_without_changing_home_branch():
    """Employee stays at Branch 1, but receives roaming access to Branch 2."""
    emp_before = get_employee_by_id(101)
    assert emp_before["branchId"] == 1

    res = grant_multi_branch_access(
        employee_id=101,
        target_branch_id=2,
        simulate=True
    )
    assert res["ok"] is True
    emp_after = res["employee"]
    assert emp_after["branchId"] == 1  # Branch unchanged!

    # Has access to both devices in list
    devices = [a["deviceSn"] for a in emp_after["access"]]
    assert "NFZ8254900401" in devices
    assert "UFZ9876543210" in devices


def test_revoke_device_access():
    """Revoke biometric access deletes user from that specific reader."""
    res = revoke_employee_device_access(
        employee_id=101,
        device_sn="NFZ8254900401",
        simulate=True
    )
    assert res["ok"] is True
    assert res["deleted"] is True

    src_users = [u["user_id"] for u in SIMULATED_DEVICES["NFZ8254900401"]["users"]]
    assert "101" not in src_users


def test_api_employees_endpoints():
    """Test REST API endpoints for employees."""
    # List employees
    resp = client.get("/api/employees")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 3

    # Transfer employee via API
    resp_transfer = client.post("/api/employees/transfer", json={
        "employeeId": "101",
        "toBranchId": 2,
        "transferBiometric": True,
        "biometricMode": "copy",
        "simulate": True
    })
    assert resp_transfer.status_code == 200
    transfer_data = resp_transfer.json()
    assert transfer_data["ok"] is True
    assert transfer_data["employee"]["branchId"] == 2
    assert transfer_data["biometric"]["ok"] is True

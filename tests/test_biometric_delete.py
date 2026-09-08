"""
Pytest unit tests for Biometric Deletion, Offboarding, and Target Device Cleanup.
"""
import pytest
from app.core.device_migration import (
    delete_batch_device_users,
    delete_device_user,
    fetch_device_users,
    reset_simulation_state,
    run_migration,
)
from app.core.employee_service import (
    delete_employee_permanently,
    fire_employee_and_wipe_biometrics,
    revoke_employee_device_access,
)


@pytest.fixture(autouse=True)
def setup_simulation():
    reset_simulation_state()


def test_delete_copied_user_from_target_device_only():
    """Verify copying a user to target, then deleting from target leaves source intact."""
    src_sn = "NFZ8254900401"
    tgt_sn = "UFZ9876543210"

    # Copy user 101 to target
    res = run_migration(source_ip=src_sn, target_ip=tgt_sn, user_ids=["101"], mode="copy", simulate=True)
    assert all(r.get("ok") for r in res) is True

    # Verify user exists on target
    tgt_users = fetch_device_users(tgt_sn, simulate=True)["users"]
    assert any(str(u["userId"]) == "101" for u in tgt_users)

    # Delete copied user from target
    del_res = delete_device_user(tgt_sn, "101", simulate=True)
    assert del_res["ok"] is True
    assert del_res["deleted"] is True

    # Verify user is removed from target but preserved on source
    tgt_users_after = fetch_device_users(tgt_sn, simulate=True)["users"]
    src_users_after = fetch_device_users(src_sn, simulate=True)["users"]

    assert not any(str(u["userId"]) == "101" for u in tgt_users_after)
    assert any(str(u["userId"]) == "101" for u in src_users_after)


def test_delete_biometrics_only():
    """Verify biometrics_only clears fingerprints but retains user PIN."""
    tgt_sn = "UFZ9876543210"
    run_migration(source_ip="NFZ8254900401", target_ip=tgt_sn, user_ids=["101"], mode="copy", simulate=True)

    del_res = delete_device_user(tgt_sn, "101", delete_biometrics_only=True, simulate=True)
    assert del_res["ok"] is True
    assert del_res["biometricsOnly"] is True

    tgt_users = fetch_device_users(tgt_sn, simulate=True)["users"]
    u = next(u for u in tgt_users if str(u["userId"]) == "101")
    assert u["fingerCount"] == 0


def test_batch_delete_users():
    """Verify batch deletion removes multiple users in one call."""
    src_sn = "NFZ8254900401"
    res = delete_batch_device_users(src_sn, ["101", "102"], simulate=True)
    assert res["ok"] is True
    assert res["deletedCount"] == 2

    src_users = fetch_device_users(src_sn, simulate=True)["users"]
    assert not any(str(u["userId"]) in ["101", "102"] for u in src_users)


def test_fire_employee_and_wipe_all_devices():
    """Verify offboarding/firing an employee wipes biometrics from all readers and sets status to Terminated."""
    fire_res = fire_employee_and_wipe_biometrics(
        employee_id=101,
        action="fire_all",
        delete_mode="full",
        simulate=True
    )
    assert fire_res["ok"] is True
    assert fire_res["employee"]["status"] == "Terminated"
    assert len(fire_res["employee"]["access"]) == 0

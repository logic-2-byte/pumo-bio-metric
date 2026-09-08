"""
Tests for Biometric Device-to-Device Migration Engine and APIs.

ZERO DATABASE DEPENDENCY:
All tests run in-memory and against device mocks/simulators.
"""
from fastapi.testclient import TestClient

from app.core.device_migration import (
    SIMULATED_DEVICES,
    fetch_device_users,
    migrate_single_user_simulated,
    probe_device_connection,
    reset_simulation_state,
    run_migration,
)
from app.main import app

client = TestClient(app)


def setup_function():
    """Reset simulated devices before each test using real employee records."""
    reset_simulation_state()


def test_device_connection_probe():
    """Probe connectivity to simulated device returns correct device specs."""
    res = probe_device_connection("192.168.1.209", 4370, simulate=True)
    assert res["ok"] is True
    assert res["userCount"] == 3
    assert res["fpVersion"] == 10
    assert "serialNumber" in res


def test_fetch_device_users():
    """Listing users returns accurate counts of users and enrolled fingers with real names."""
    res = fetch_device_users("192.168.1.209", 4370, simulate=True)
    assert res["ok"] is True
    assert res["count"] == 3
    users_by_id = {u["userId"]: u for u in res["users"]}

    assert users_by_id["101"]["name"] == "Member 1o"
    assert users_by_id["101"]["fingerCount"] == 2
    assert users_by_id["101"]["fingerFids"] == [0, 1]

    assert users_by_id["102"]["name"] == "Gowtham"
    assert users_by_id["102"]["fingerCount"] == 1

    assert users_by_id["111"]["name"] == "ARULAJAY"
    assert users_by_id["111"]["fingerCount"] == 0
    assert users_by_id["111"]["roleLabel"] == "Super Admin"


def test_migration_copy_mode_with_fingerprints():
    """Copy mode transfers user and all fingerprints, keeping source user intact."""
    result = migrate_single_user_simulated(
        source_ip="192.168.1.209",
        target_ip="192.168.1.210",
        user_id="101",
        mode="copy"
    )

    assert result.ok is True
    assert result.fingers_transferred == 2
    assert result.user_name == "Member 1o"

    # Verify source device still has user 101
    src_users = [u["user_id"] for u in SIMULATED_DEVICES["192.168.1.209"]["users"]]
    assert "101" in src_users

    # Verify target device now has user 101 with both fingers
    tgt_user = next((u for u in SIMULATED_DEVICES["192.168.1.210"]["users"] if u["user_id"] == "101"), None)
    assert tgt_user is not None
    assert tgt_user["name"] == "Member 1o"
    assert len(tgt_user["fingers"]) == 2
    assert tgt_user["uid"] != 1  # Should avoid UID 1 which belonged to 9999


def test_migration_move_mode_deletes_from_source():
    """Move mode transfers user and fingerprints to target, and deletes from source."""
    result = migrate_single_user_simulated(
        source_ip="192.168.1.209",
        target_ip="192.168.1.210",
        user_id="102",
        mode="move"
    )

    assert result.ok is True
    assert result.fingers_transferred == 1
    assert result.user_name == "Gowtham"

    # Verify target has user 102
    tgt_users = [u["user_id"] for u in SIMULATED_DEVICES["192.168.1.210"]["users"]]
    assert "102" in tgt_users

    # Verify source NO LONGER has user 102
    src_users = [u["user_id"] for u in SIMULATED_DEVICES["192.168.1.209"]["users"]]
    assert "102" not in src_users


def test_migration_zero_fingerprint_user():
    """User with only card/PIN (0 fingerprints) migrates without errors."""
    result = migrate_single_user_simulated(
        source_ip="192.168.1.209",
        target_ip="192.168.1.210",
        user_id="111",
        mode="copy"
    )

    assert result.ok is True
    assert result.fingers_transferred == 0
    tgt_user = next((u for u in SIMULATED_DEVICES["192.168.1.210"]["users"] if u["user_id"] == "111"), None)
    assert tgt_user is not None
    assert tgt_user["privilege"] == 14  # Super Admin preserved


def test_migration_nonexistent_user_fails_gracefully():
    """Attempting to migrate non-existent user returns clean failure with log."""
    result = migrate_single_user_simulated(
        source_ip="192.168.1.209",
        target_ip="192.168.1.210",
        user_id="999",
        mode="copy"
    )

    assert result.ok is False
    assert "not found" in result.error.lower()


# -----------------------------------------------------------------------------
# FastAPI HTTP API Endpoints Tests
# -----------------------------------------------------------------------------

def test_api_test_connection_endpoint():
    resp = client.post("/api/migration/test-connection", json={
        "ip": "192.168.1.209",
        "port": 4370,
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["userCount"] == 3


def test_api_users_endpoint():
    resp = client.post("/api/migration/users", json={
        "ip": "192.168.1.209",
        "port": 4370,
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["count"] == 3
    assert any(u["userId"] == "101" for u in data["users"])


def test_api_run_migration_endpoint():
    resp = client.post("/api/migration/run", json={
        "sourceIp": "192.168.1.209",
        "targetIp": "192.168.1.210",
        "userIds": ["101"],
        "mode": "copy",
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["userId"] == "101"
    assert data["results"][0]["fingersTransferred"] == 2


def test_api_reset_sim_endpoint():
    resp = client.post("/api/migration/reset-sim")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_get_devices_list():
    """Verify GET /api/migration/devices returns serial numbers."""
    resp = client.get("/api/migration/devices")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 2
    serials = [d["sn"] for d in data["devices"]]
    assert "NFZ8254900401" in serials
    assert "UFZ9876543210" in serials


def test_api_run_migration_with_serial_numbers():
    """Verify migration execution works directly with Serial Numbers."""
    resp = client.post("/api/migration/run", json={
        "sourceSn": "NFZ8254900401",
        "targetSn": "UFZ9876543210",
        "userIds": ["101"],
        "mode": "copy",
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["userId"] == "101"
    assert data["results"][0]["fingersTransferred"] == 2
    assert "NFZ8254900401" in data["results"][0]["sourceIp"] or "NFZ8254900401" in "".join(data["results"][0]["logs"])


def test_delete_device_user_simulated():
    """Verify delete_device_user removes user from device."""
    from app.core.device_migration import delete_device_user
    del_res = delete_device_user("NFZ8254900401", "102", simulate=True)
    assert del_res["ok"] is True
    assert del_res["deleted"] is True

    # User 102 should no longer exist on NFZ8254900401
    users = [u["user_id"] for u in SIMULATED_DEVICES["NFZ8254900401"]["users"]]
    assert "102" not in users

    # Deleting again should report deleted=False
    del_res2 = delete_device_user("NFZ8254900401", "102", simulate=True)
    assert del_res2["ok"] is True
    assert del_res2["deleted"] is False


def test_api_delete_device_user_endpoint():
    """Verify POST /api/migration/delete endpoint."""
    resp = client.post("/api/migration/delete", json={
        "sn": "NFZ8254900401",
        "userId": "101",
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["deleted"] is True


def test_pin_collision_resolution():
    """Verify PIN mapping when destination PIN is occupied."""
    from app.core.device_migration import resolve_target_pin
    # UFZ9876543210 initially has user '9999'
    res_free = resolve_target_pin("UFZ9876543210", "101", simulate=True)
    assert res_free["ok"] is True
    assert res_free["targetPin"] == "101"
    assert res_free["isMapped"] is False

    # Check collision when preferred PIN is 9999 (occupied by Master Super Admin)
    res_collide = resolve_target_pin("UFZ9876543210", "9999", user_name="John Doe", simulate=True)
    assert res_collide["ok"] is True
    assert res_collide["isMapped"] is True
    assert res_collide["targetPin"] != "9999"


def test_api_transfer_single_user_endpoint():
    """Verify POST /api/migration/transfer-user endpoint with copy and move."""
    resp = client.post("/api/migration/transfer-user", json={
        "sourceSn": "NFZ8254900401",
        "targetSn": "UFZ9876543210",
        "userId": "101",
        "mode": "copy",
        "simulate": True
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["userId"] == "101"
    assert data["targetUserId"] == "101"
    assert data["fingersTransferred"] == 2


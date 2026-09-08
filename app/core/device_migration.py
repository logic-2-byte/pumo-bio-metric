"""
Core Biometric Device-to-Device Migration Engine.

Handles extracting employee records (employee code/user_id, name, privilege,
card, password, group_id) and all enrolled biometric fingerprint templates from
a source ZK/eSSL device, and enrolling them onto a target ZK/eSSL device over TCP 4370.

Supports:
- Copy mode: duplicate employee + fingerprints to target device (leaves source untouched).
- Move mode: verifies employee + fingerprints on target device, then removes from source device.
- Standalone / Zero Database: runs entirely via direct TCP sockets and in-memory caches.
- Built-in Simulator: allows testing the UI and migration workflow even when physical
  hardware is not currently connected to the local network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Try importing pyzk; fallback gracefully if not present
try:
    from zk import ZK
    from zk.finger import Finger
    from zk.user import User
    HAS_PYZK = True
except ImportError:
    HAS_PYZK = False
    ZK = None  # type: ignore
    User = None  # type: ignore
    Finger = None  # type: ignore


import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DEVICE_REGISTRY_FILE = os.path.join(BASE_DIR, "device_registry.json")


def get_registered_devices() -> dict[str, dict[str, Any]]:
    """Load persistent device registry mapping SN -> device metadata."""
    registry = {
        "NFZ8254900401": {"sn": "NFZ8254900401", "ip": "192.168.0.210", "port": 4370, "name": "eSSL SilkBio-101TC (NFZ8254900401)"},
        "NFZ8242802542": {"sn": "NFZ8242802542", "ip": "192.168.0.154", "port": 4370, "name": "eSSL x 2008 (NFZ8242802542)"},
        "UFZ9876543210": {"sn": "UFZ9876543210", "ip": "192.168.0.154", "port": 4370, "name": "eSSL uFace 202 (UFZ9876543210)"},
        "ZK1": {"sn": "ZK1", "ip": "192.168.1.209", "port": 4370, "name": "eSSL Terminal (ZK1)"},
    }
    if os.path.exists(DEVICE_REGISTRY_FILE):
        try:
            with open(DEVICE_REGISTRY_FILE, encoding="utf-8") as f:
                registry.update(json.load(f))
        except Exception:
            pass
    return registry


# -----------------------------------------------------------------------------
# Simulated in-memory devices for testing UI without physical readers
# Indexed by both Serial Number and IP
# -----------------------------------------------------------------------------
_SIM_SOURCE = {
    "name": "eSSL SilkBio-101TC (NFZ8254900401)",
    "sn": "NFZ8254900401",
    "ip": "192.168.0.210",
    "port": 4370,
    "fp_version": 10,
    "platform": "ZMM220",
    "users": [
        {
            "uid": 1,
            "user_id": "101",
            "name": "Member 1o",
            "privilege": 0,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"TEMPLATE_FINGER_0_USER_101_SAMPLE"},
                {"fid": 1, "valid": 1, "template": b"TEMPLATE_FINGER_1_USER_101_SAMPLE"}
            ]
        },
        {
            "uid": 2,
            "user_id": "102",
            "name": "Gowtham",
            "privilege": 0,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"TEMPLATE_FINGER_0_USER_102_SAMPLE"}
            ]
        },
        {
            "uid": 3,
            "user_id": "111",
            "name": "ARULAJAY",
            "privilege": 14,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": []
        }
    ]
}

_SIM_TARGET = {
    "name": "eSSL uFace 202 (Floor 2)",
    "sn": "UFZ9876543210",
    "ip": "192.168.0.154",
    "port": 4370,
    "fp_version": 10,
    "platform": "ZMM220",
    "users": [
        {
            "uid": 1,
            "user_id": "9999",
            "name": "Master Super Admin",
            "privilege": 14,
            "password": "",
            "group_id": "1",
            "card": 999999,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"MOCK_TEMPLATE_FINGER_0_USER_9999_HEX_DATA_SAMPLE"}
            ]
        }
    ]
}

SIMULATED_DEVICES: dict[str, dict[str, Any]] = {
    "NFZ8254900401": _SIM_SOURCE,
    "192.168.0.210": _SIM_SOURCE,
    "192.168.1.209": _SIM_SOURCE,
    "UFZ9876543210": _SIM_TARGET,
    "NFZ8242802542": _SIM_TARGET,
    "192.168.0.154": _SIM_TARGET,
    "192.168.1.210": _SIM_TARGET,
}


def reset_simulation_state():
    """Reset simulated devices to default initial state using real employee records."""
    global _SIM_SOURCE, _SIM_TARGET, SIMULATED_DEVICES
    _SIM_SOURCE["users"] = [
        {
            "uid": 1,
            "user_id": "101",
            "name": "Member 1o",
            "privilege": 0,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"TEMPLATE_FINGER_0_USER_101_SAMPLE"},
                {"fid": 1, "valid": 1, "template": b"TEMPLATE_FINGER_1_USER_101_SAMPLE"}
            ]
        },
        {
            "uid": 2,
            "user_id": "102",
            "name": "Gowtham",
            "privilege": 0,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"TEMPLATE_FINGER_0_USER_102_SAMPLE"}
            ]
        },
        {
            "uid": 3,
            "user_id": "111",
            "name": "ARULAJAY",
            "privilege": 14,
            "password": "",
            "group_id": "1",
            "card": 0,
            "fingers": []
        }
    ]
    _SIM_TARGET["users"] = [
        {
            "uid": 1,
            "user_id": "9999",
            "name": "Master Super Admin",
            "privilege": 14,
            "password": "",
            "group_id": "1",
            "card": 999999,
            "fingers": [
                {"fid": 0, "valid": 1, "template": b"MOCK_TEMPLATE_FINGER_0_USER_9999_HEX_DATA_SAMPLE"}
            ]
        }
    ]
    SIMULATED_DEVICES["NFZ8254900401"] = _SIM_SOURCE
    SIMULATED_DEVICES["192.168.0.210"] = _SIM_SOURCE
    SIMULATED_DEVICES["192.168.1.209"] = _SIM_SOURCE
    SIMULATED_DEVICES["UFZ9876543210"] = _SIM_TARGET
    SIMULATED_DEVICES["192.168.0.154"] = _SIM_TARGET
    SIMULATED_DEVICES["192.168.1.210"] = _SIM_TARGET


def resolve_device(identifier: str, default_port: int = 4370) -> dict[str, Any]:
    """
    Resolve a device by Serial Number (e.g. 'NFZ8254900401') or IP address.
    Returns: {'sn': ..., 'ip': ..., 'port': ..., 'name': ...}
    """
    clean_id = identifier.strip()
    registry = get_registered_devices()

    # 1. Match by Serial Number directly
    if clean_id in registry:
        info = dict(registry[clean_id])
        info.setdefault("port", default_port)
        return info

    # 2. Case-insensitive SN match
    for k, v in registry.items():
        if k.lower() == clean_id.lower():
            info = dict(v)
            info.setdefault("port", default_port)
            return info

    # 3. Match by IP in registry
    for k, v in registry.items():
        if v.get("ip") == clean_id:
            info = dict(v)
            info.setdefault("port", default_port)
            return info

    # 4. Check if it's in simulated devices
    if clean_id in SIMULATED_DEVICES:
        dev = SIMULATED_DEVICES[clean_id]
        return {
            "sn": dev["sn"],
            "ip": dev.get("ip", clean_id),
            "port": dev.get("port", default_port),
            "name": dev["name"]
        }

    # 5. Fallback: treat as standalone Serial Number or IP
    if "." in clean_id and not any(c.isalpha() for c in clean_id):
        # Likely an IP
        return {"sn": f"DEV-{clean_id}", "ip": clean_id, "port": default_port, "name": f"Device {clean_id}"}
    return {"sn": clean_id, "ip": clean_id, "port": default_port, "name": f"Device {clean_id}"}


@dataclass
class MigrationResult:
    ok: bool
    mode: str
    user_id: str
    user_name: str
    source_ip: str
    target_ip: str
    target_user_id: str = ""
    fingers_transferred: int = 0
    logs: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "userId": self.user_id,
            "targetUserId": self.target_user_id or self.user_id,
            "userName": self.user_name,
            "sourceIp": self.source_ip,
            "targetIp": self.target_ip,
            "fingersTransferred": self.fingers_transferred,
            "logs": self.logs,
            "error": self.error,
        }


def connect_zk_device(ip: str, port: int = 4370, timeout: int = 5):
    """Safely connect to a real biometric device using pyzk."""
    if not HAS_PYZK:
        raise RuntimeError("The 'pyzk' library is not installed in this environment.")

    zk = ZK(
        ip,
        port=port,
        timeout=timeout,
        force_udp=False,
        ommit_ping=True
    )
    conn = zk.connect()
    return zk, conn


USER_NAMES_FILE = os.path.join(BASE_DIR, "user_names.json")


def get_employee_info(user_id: str) -> dict[str, Any]:
    """Retrieve known real employee name and role from user_names.json."""
    u_id = str(user_id).strip()
    if os.path.exists(USER_NAMES_FILE):
        try:
            with open(USER_NAMES_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if u_id in data:
                    val = data[u_id]
                    if isinstance(val, dict):
                        return val
                    return {"name": str(val), "role": "Normal User"}
        except Exception:
            pass
    return {"name": f"User {u_id}", "role": "Normal User"}


def probe_device_connection(device_id: str, port: int = 4370, timeout: int = 5, simulate: bool = False) -> dict[str, Any]:
    """
    Probe TCP connectivity to a device by Serial Number or IP, and fetch specs.
    Zero database dependency.
    """
    resolved = resolve_device(device_id, port)
    target_sn = resolved["sn"]
    target_ip = resolved["ip"]
    target_port = resolved["port"]
    target_name = resolved.get("name", f"Reader ({target_sn})")

    if simulate:
        dev = SIMULATED_DEVICES.get(target_sn) or SIMULATED_DEVICES.get(target_ip, {
            "name": target_name,
            "sn": target_sn,
            "fp_version": 10,
            "platform": "SimPlatform-v1",
            "users": []
        })
        user_count = len(dev.get("users", []))
        finger_count = sum(len(u.get("fingers", [])) for u in dev.get("users", []))
        return {
            "ok": True,
            "simulated": True,
            "ip": target_ip,
            "port": target_port,
            "deviceName": dev.get("name", target_name),
            "serialNumber": dev.get("sn", target_sn),
            "userCount": user_count,
            "fingerCount": finger_count,
            "fpVersion": dev.get("fp_version", 10),
            "platform": dev.get("platform", "Simulator"),
            "message": f"Connected successfully to simulated device {target_sn}."
        }

    # Real hardware connection
    if not HAS_PYZK:
        return {
            "ok": False,
            "ip": target_ip,
            "port": target_port,
            "serialNumber": target_sn,
            "error": "The 'pyzk' package is not installed."
        }

    conn = None
    try:
        _, conn = connect_zk_device(target_ip, target_port, timeout)
        conn.disable_device()
        try:
            device_name = conn.get_device_name() or target_name
        except Exception:
            device_name = target_name

        try:
            sn = conn.get_serialnumber() or target_sn
        except Exception:
            sn = target_sn

        try:
            fp_version = conn.get_fp_version()
        except Exception:
            fp_version = 10

        try:
            platform = conn.get_platform() or "Unknown"
        except Exception:
            platform = "N/A"

        try:
            users = conn.get_users()
            user_count = len(users)
        except Exception:
            user_count = 0

        try:
            templates = conn.get_templates()
            finger_count = len(templates)
        except Exception:
            finger_count = 0

        conn.enable_device()
        return {
            "ok": True,
            "simulated": False,
            "ip": target_ip,
            "port": target_port,
            "deviceName": str(device_name),
            "serialNumber": str(sn),
            "userCount": user_count,
            "fingerCount": finger_count,
            "fpVersion": fp_version,
            "platform": str(platform),
            "message": f"Connected successfully to {device_name} ({sn})"
        }
    except Exception as e:
        return {
            "ok": False,
            "simulated": False,
            "ip": target_ip,
            "port": target_port,
            "error": f"Failed to connect to {target_ip}:{target_port} - {e!s}"
        }
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass


def fetch_device_users(device_id: str, port: int = 4370, timeout: int = 5, simulate: bool = False) -> dict[str, Any]:
    """
    Fetch all users and their enrolled finger count from a device by Serial Number or IP.
    Zero database dependency.
    """
    resolved = resolve_device(device_id, port)
    target_sn = resolved["sn"]
    target_ip = resolved["ip"]
    target_port = resolved["port"]
    target_name = resolved.get("name", f"Reader ({target_sn})")

    if simulate:
        dev = SIMULATED_DEVICES.get(target_sn) or SIMULATED_DEVICES.get(target_ip)
        if not dev:
            return {"ok": False, "error": f"Simulated device {target_sn} not found"}

        users_list = []
        for u in dev.get("users", []):
            emp_info = get_employee_info(u["user_id"])
            emp_name = emp_info["name"] if not emp_info["name"].startswith("User ") else u["name"]
            users_list.append({
                "uid": u["uid"],
                "userId": str(u["user_id"]),
                "name": emp_name,
                "privilege": u["privilege"],
                "roleLabel": "Super Admin" if u["privilege"] == 14 else ("Manager" if u["privilege"] == 2 else "Normal User"),
                "card": u.get("card", 0),
                "fingerCount": len(u.get("fingers", [])),
                "fingerFids": [f["fid"] for f in u.get("fingers", [])]
            })
        return {
            "ok": True,
            "simulated": True,
            "serialNumber": target_sn,
            "ip": target_ip,
            "count": len(users_list),
            "users": users_list
        }

    if not HAS_PYZK:
        return {"ok": False, "error": "The 'pyzk' package is not installed."}

    conn = None
    try:
        _, conn = connect_zk_device(target_ip, target_port, timeout)
        conn.disable_device()

        users = conn.get_users()
        templates = []
        try:
            templates = conn.get_templates()
        except Exception:
            templates = []

        # Count fingers per uid
        finger_map: dict[int, list[int]] = {}
        for t in templates:
            finger_map.setdefault(t.uid, []).append(t.fid)

        users_list = []
        for u in users:
            fids = finger_map.get(u.uid, [])
            role_label = "Super Admin" if u.privilege == 14 else ("Manager" if u.privilege == 2 else "Normal User")
            emp_info = get_employee_info(u.user_id)
            emp_name = emp_info["name"] if not emp_info["name"].startswith("User ") else (u.name or f"User {u.user_id}")
            users_list.append({
                "uid": u.uid,
                "userId": str(u.user_id),
                "name": emp_name,
                "privilege": u.privilege,
                "roleLabel": role_label,
                "card": u.card,
                "fingerCount": len(fids),
                "fingerFids": fids
            })

        conn.enable_device()
        return {
            "ok": True,
            "simulated": False,
            "serialNumber": target_sn,
            "ip": target_ip,
            "count": len(users_list),
            "users": users_list
        }
    except Exception as e:
        return {"ok": False, "error": f"Failed to fetch users from {target_sn} ({target_ip}:{target_port}): {e}"}
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass


def delete_device_user_simulated(
    device_id: str,
    user_id: str,
    delete_biometrics_only: bool = False
) -> dict[str, Any]:
    """Delete a user and all their enrolled fingerprints (or fingerprints only) from an in-memory simulated device."""
    resolved = resolve_device(device_id)
    sn = resolved["sn"]
    ip = resolved["ip"]
    dev = SIMULATED_DEVICES.get(sn) or SIMULATED_DEVICES.get(ip)
    if not dev:
        return {"ok": False, "error": f"Simulated device {sn} not found"}

    users = dev.get("users", [])
    target = next((u for u in users if str(u.get("user_id")) == str(user_id)), None)
    if not target:
        return {
            "ok": True,
            "simulated": True,
            "deleted": False,
            "device": sn,
            "userId": str(user_id),
            "message": f"User {user_id} not found on simulated device {sn}."
        }

    if delete_biometrics_only:
        target["fingers"] = []
        target["finger_count"] = 0
        return {
            "ok": True,
            "simulated": True,
            "deleted": True,
            "biometricsOnly": True,
            "device": sn,
            "userId": str(user_id),
            "message": f"Enrolled fingerprint templates cleared for user {user_id} on simulated device {sn}."
        }

    dev["users"] = [u for u in users if str(u.get("user_id")) != str(user_id)]
    return {
        "ok": True,
        "simulated": True,
        "deleted": True,
        "biometricsOnly": False,
        "device": sn,
        "userId": str(user_id),
        "message": f"User {user_id} and all biometric templates deleted successfully from simulated device {sn}."
    }


def delete_device_user_real(
    device_id: str,
    user_id: str,
    port: int = 4370,
    timeout: int = 5,
    delete_biometrics_only: bool = False,
    ip_override: str | None = None
) -> dict[str, Any]:
    """Delete a user and all their enrolled fingerprints directly from a real hardware reader over TCP 4370."""
    resolved = resolve_device(device_id, port)
    sn = resolved["sn"]
    ip = ip_override if ip_override and "." in ip_override else resolved["ip"]
    port = resolved["port"]

    if not HAS_PYZK:
        return {"ok": False, "error": "The 'pyzk' package is not installed."}

    conn = None
    try:
        _, conn = connect_zk_device(ip, port, timeout)
        conn.disable_device()
        users = conn.get_users()
        target_user = next((u for u in users if str(u.user_id) == str(user_id)), None)
        if not target_user:
            conn.enable_device()
            return {
                "ok": True,
                "simulated": False,
                "deleted": False,
                "device": sn,
                "userId": str(user_id),
                "message": f"User {user_id} was not found on device {sn}."
            }

        if delete_biometrics_only:
            deleted_templates = 0
            for fid in range(10):
                success = False
                try:
                    # In pyzk TCP mode, delete_user_template has a Python 3 bug where user_id str
                    # causes struct.error on pack('<24sB', user_id, temp_id).
                    # Calling with user_id='' safely triggers CMD_DELETE_USERTEMP with integer pack('hb', uid, fid).
                    if conn.delete_user_template(uid=target_user.uid, temp_id=fid, user_id=''):
                        success = True
                except Exception:
                    pass

                if not success:
                    try:
                        from struct import pack
                        cmd_str = pack('<24sB', str(user_id).encode('ascii', errors='ignore'), fid)
                        res = conn._ZK__send_command(134, cmd_str)
                        if res and res.get('status'):
                            success = True
                    except Exception:
                        pass

                if success:
                    deleted_templates += 1
            conn.refresh_data()
            conn.enable_device()
            return {
                "ok": True,
                "simulated": False,
                "deleted": True,
                "biometricsOnly": True,
                "templatesDeleted": deleted_templates,
                "device": sn,
                "userId": str(user_id),
                "message": f"Biometric fingerprint templates cleared for user {user_id} (UID: {target_user.uid}) on device {sn}."
            }

        conn.delete_user(uid=target_user.uid, user_id=str(user_id))
        conn.refresh_data()
        conn.enable_device()
        return {
            "ok": True,
            "simulated": False,
            "deleted": True,
            "biometricsOnly": False,
            "device": sn,
            "userId": str(user_id),
            "message": f"Successfully deleted user {user_id} (UID: {target_user.uid}) from device {sn}."
        }
    except Exception as e:
        logger.warning("TCP socket port 4370 connect failed for device %s (%s). Falling back to ADMS Push protocol command queue: %s", sn, ip, e)
        try:
            from app.router.iclock import queue_device_cmd
            u_pin = str(user_id).strip()
            if delete_biometrics_only:
                c1 = queue_device_cmd(sn, f"DATA DELETE FINGERTMP PIN={u_pin}")
                c2 = queue_device_cmd(sn, f"DATA DELETE BIOPHOTO PIN={u_pin}")
                return {
                    "ok": True,
                    "simulated": False,
                    "deleted": True,
                    "queued": True,
                    "biometricsOnly": True,
                    "device": sn,
                    "userId": u_pin,
                    "message": f"Biometric fingerprint wipe command queued for {sn} via ADMS push protocol ({c1}, {c2}). Machine will execute on next poll."
                }
            else:
                c1 = queue_device_cmd(sn, f"DATA DELETE USER PIN={u_pin}")
                c2 = queue_device_cmd(sn, f"DATA DELETE FINGERTMP PIN={u_pin}")
                c3 = queue_device_cmd(sn, f"DATA DELETE BIOPHOTO PIN={u_pin}")
                return {
                    "ok": True,
                    "simulated": False,
                    "deleted": True,
                    "queued": True,
                    "biometricsOnly": False,
                    "device": sn,
                    "userId": u_pin,
                    "message": f"User delete command queued for {sn} via ADMS push protocol ({c1}). Machine will execute on next poll."
                }
        except Exception as q_err:
            logger.exception("Failed to queue ADMS push delete command: %s", q_err)
            return {"ok": False, "simulated": False, "device": sn, "userId": str(user_id), "error": str(e)}
    finally:
        if conn:
            try:
                conn.enable_device()
            except Exception:
                pass
            try:
                conn.disconnect()
            except Exception:
                pass


def delete_device_user(
    device_id: str,
    user_id: str,
    port: int = 4370,
    timeout: int = 5,
    delete_biometrics_only: bool = False,
    simulate: bool = False,
    ip_override: str | None = None
) -> dict[str, Any]:
    """Delete a user and their biometric fingerprints from a physical reader or mock device."""
    if simulate:
        return delete_device_user_simulated(device_id, user_id, delete_biometrics_only=delete_biometrics_only)
    return delete_device_user_real(device_id, user_id, port, timeout, delete_biometrics_only=delete_biometrics_only, ip_override=ip_override)


def delete_batch_device_users(
    device_id: str,
    user_ids: list[str],
    port: int = 4370,
    timeout: int = 8,
    delete_biometrics_only: bool = False,
    simulate: bool = False,
    ip_override: str | None = None
) -> dict[str, Any]:
    """Delete multiple users / biometric templates in a single connection session."""
    if not user_ids:
        return {"ok": True, "deletedCount": 0, "results": []}

    if simulate:
        results = []
        for uid in user_ids:
            res = delete_device_user_simulated(device_id, uid, delete_biometrics_only=delete_biometrics_only)
            results.append(res)
        deleted_count = sum(1 for r in results if r.get("deleted"))
        return {
            "ok": True,
            "simulated": True,
            "deletedCount": deleted_count,
            "totalRequested": len(user_ids),
            "results": results,
            "message": f"Successfully deleted {deleted_count}/{len(user_ids)} users from simulated device."
        }

    resolved = resolve_device(device_id, port)
    sn = resolved["sn"]
    ip = ip_override if ip_override and "." in ip_override else resolved["ip"]
    port = resolved["port"]

    if not HAS_PYZK:
        return {"ok": False, "error": "The 'pyzk' package is not installed."}

    conn = None
    results = []
    try:
        _, conn = connect_zk_device(ip, port, timeout)
        conn.disable_device()
        users = conn.get_users()
        user_map = {str(u.user_id): u for u in users}

        for uid in user_ids:
            u_str = str(uid).strip()
            target_user = user_map.get(u_str)
            if not target_user:
                results.append({
                    "ok": True,
                    "deleted": False,
                    "userId": u_str,
                    "message": f"User {u_str} not found on device {sn}."
                })
                continue

            try:
                if delete_biometrics_only:
                    for fid in range(10):
                        try:
                            conn.delete_user_template(uid=target_user.uid, temp_id=fid, user_id=u_str)
                        except Exception:
                            pass
                    results.append({
                        "ok": True,
                        "deleted": True,
                        "biometricsOnly": True,
                        "userId": u_str,
                        "message": f"Biometrics cleared for {u_str} on {sn}."
                    })
                else:
                    conn.delete_user(uid=target_user.uid, user_id=u_str)
                    results.append({
                        "ok": True,
                        "deleted": True,
                        "biometricsOnly": False,
                        "userId": u_str,
                        "message": f"User {u_str} deleted from {sn}."
                    })
            except Exception as item_err:
                results.append({
                    "ok": False,
                    "deleted": False,
                    "userId": u_str,
                    "error": str(item_err)
                })

        conn.refresh_data()
        conn.enable_device()
        deleted_count = sum(1 for r in results if r.get("deleted"))
        return {
            "ok": True,
            "simulated": False,
            "device": sn,
            "deletedCount": deleted_count,
            "totalRequested": len(user_ids),
            "results": results,
            "message": f"Deleted {deleted_count}/{len(user_ids)} users from device {sn}."
        }
    except Exception as e:
        logger.exception("Failed batch delete on device %s: %s", sn, e)
        return {"ok": False, "simulated": False, "device": sn, "error": str(e), "results": results}
    finally:
        if conn:
            try:
                conn.enable_device()
            except Exception:
                pass
            try:
                conn.disconnect()
            except Exception:
                pass


def resolve_target_pin(
    target_id: str,
    preferred_pin: str,
    user_name: str | None = None,
    port: int = 4370,
    timeout: int = 5,
    simulate: bool = False
) -> dict[str, Any]:
    """
    Check if preferred_pin is occupied on the target device.
    If free or belongs to the same user, keep it.
    If colliding with someone else, find the next available integer PIN.
    """
    pref_str = str(preferred_pin).strip()
    resolved = resolve_device(target_id, port)
    target_sn = resolved["sn"]

    existing_users: list[dict[str, Any]] = []
    if simulate:
        dev = SIMULATED_DEVICES.get(target_sn) or SIMULATED_DEVICES.get(resolved["ip"], {})
        existing_users = [
            {"userId": str(u["user_id"]), "name": u.get("name", "")}
            for u in dev.get("users", [])
        ]
    else:
        fetch_res = fetch_device_users(target_id, port=port, timeout=timeout, simulate=False)
        if fetch_res.get("ok"):
            existing_users = [
                {"userId": str(u["userId"]), "name": u.get("name", "")}
                for u in fetch_res.get("users", [])
            ]

    match = next((u for u in existing_users if u["userId"] == pref_str), None)
    if not match:
        return {
            "ok": True,
            "device": target_sn,
            "preferredPin": pref_str,
            "targetPin": pref_str,
            "isMapped": False,
            "reason": "PIN is free on target device"
        }

    # If it matches the same user name, no collision
    if user_name and match["name"].strip().lower() == user_name.strip().lower():
        return {
            "ok": True,
            "device": target_sn,
            "preferredPin": pref_str,
            "targetPin": pref_str,
            "isMapped": False,
            "reason": f"PIN matches existing employee '{match['name']}' on target device"
        }

    # Collision! Find next available PIN
    used_pins: set[int] = set()
    for u in existing_users:
        try:
            used_pins.add(int(u["userId"]))
        except (ValueError, TypeError):
            pass

    cand = int(pref_str) if pref_str.isdigit() else 1
    while cand in used_pins:
        cand += 1

    mapped_pin = str(cand)
    return {
        "ok": True,
        "device": target_sn,
        "preferredPin": pref_str,
        "targetPin": mapped_pin,
        "isMapped": True,
        "reason": f"PIN '{pref_str}' was occupied by '{match['name']}'. Auto-mapped to free PIN '{mapped_pin}'"
    }


def migrate_single_user_simulated(
    source_id: str | None = None,
    target_id: str | None = None,
    user_id: str = "",
    mode: str = "copy",
    target_user_id: str | None = None,
    *,
    source_ip: str | None = None,
    target_ip: str | None = None,
    source_sn: str | None = None,
    target_sn: str | None = None,
) -> MigrationResult:
    """Perform simulated migration between in-memory mock devices by Serial Number or IP."""
    src_identifier = source_id or source_sn or source_ip or "NFZ8254900401"
    tgt_identifier = target_id or target_sn or target_ip or "UFZ9876543210"

    src_res = resolve_device(src_identifier)
    tgt_res = resolve_device(tgt_identifier)
    src_sn = src_res["sn"]
    tgt_sn = tgt_res["sn"]
    src_ip = src_res["ip"]
    tgt_ip = tgt_res["ip"]

    logs = [f"Starting simulated migration: User {user_id} from Device {src_sn} to Device {tgt_sn} (mode: {mode.upper()})"]
    src_dev = SIMULATED_DEVICES.get(src_sn) or SIMULATED_DEVICES.get(src_ip)
    tgt_dev = SIMULATED_DEVICES.get(tgt_sn) or SIMULATED_DEVICES.get(tgt_ip)

    if not src_dev:
        return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name="N/A",
                               source_ip=src_sn, target_ip=tgt_sn, target_user_id=user_id, logs=logs,
                               error=f"Source simulated device {src_sn} not found.")
    if not tgt_dev:
        return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name="N/A",
                               source_ip=src_sn, target_ip=tgt_sn, target_user_id=user_id, logs=logs,
                               error=f"Target simulated device {tgt_sn} not found.")

    src_user = next((u for u in src_dev.get("users", []) if str(u["user_id"]) == str(user_id)), None)
    if not src_user:
        logs.append(f"User ID {user_id} not found on source device {src_sn}")
        return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name="N/A",
                               source_ip=src_sn, target_ip=tgt_sn, target_user_id=user_id, logs=logs,
                               error=f"User ID {user_id} not found on source device")

    user_name = src_user["name"]
    fingers = src_user.get("fingers", [])
    logs.append(f"Found user on source: '{user_name}' (ID: {user_id}), Role: {src_user['privilege']}, Card: {src_user.get('card', 0)}")
    logs.append(f"Found {len(fingers)} enrolled fingerprint template(s): FIDs {[f['fid'] for f in fingers]}")

    # Check target PIN mapping / collision
    dest_user_id = str(target_user_id).strip() if target_user_id else str(user_id)
    existing_tgt = next((u for u in tgt_dev.get("users", []) if str(u["user_id"]) == dest_user_id), None)
    if existing_tgt and existing_tgt.get("name") != src_user["name"]:
        # PIN Collision! Auto-find next free numeric PIN
        used_pins: set[int] = set()
        for u in tgt_dev.get("users", []):
            try:
                used_pins.add(int(u["user_id"]))
            except (ValueError, TypeError):
                pass
        cand = int(dest_user_id) if dest_user_id.isdigit() else 1
        while cand in used_pins:
            cand += 1
        dest_user_id = str(cand)
        logs.append(f"[PIN MAPPED] Target PIN '{user_id}' was occupied on {tgt_sn} by '{existing_tgt['name']}'. Auto-mapped to available PIN '{dest_user_id}'.")
        existing_tgt = None
    elif existing_tgt:
        logs.append(f"Target PIN '{dest_user_id}' matches existing user '{existing_tgt['name']}' on {tgt_sn}. Updating profile & templates.")
    else:
        logs.append(f"Target PIN '{dest_user_id}' is available on {tgt_sn}.")

    if existing_tgt and str(existing_tgt["user_id"]) == dest_user_id:
        target_uid = existing_tgt["uid"]
        existing_tgt.update({
            "name": src_user["name"],
            "privilege": src_user["privilege"],
            "password": src_user["password"],
            "group_id": src_user["group_id"],
            "card": src_user.get("card", 0),
            "fingers": [dict(f) for f in fingers]
        })
    else:
        target_uid = max([u["uid"] for u in tgt_dev.get("users", [])], default=0) + 1
        logs.append(f"User {dest_user_id} is new on target device {tgt_sn}. Assigned sequence UID: {target_uid}")
        new_tgt_user = {
            "uid": target_uid,
            "user_id": dest_user_id,
            "name": src_user["name"],
            "privilege": src_user["privilege"],
            "password": src_user["password"],
            "group_id": src_user["group_id"],
            "card": src_user.get("card", 0),
            "fingers": [dict(f) for f in fingers]
        }
        tgt_dev.setdefault("users", []).append(new_tgt_user)

    logs.append(f"Successfully saved user profile and {len(fingers)} template(s) to target device {tgt_sn} (PIN: {dest_user_id}).")
    logs.append(f"Verified user and templates on target device {tgt_sn}: OK.")

    if mode.lower() == "move":
        src_dev["users"] = [u for u in src_dev.get("users", []) if str(u["user_id"]) != str(user_id)]
        logs.append(f"Move mode: User {user_id} successfully deleted from source device {src_sn}.")

    logs.append(f"Migration completed successfully: {mode.upper()} of User {user_id} -> Target PIN {dest_user_id} ({user_name}).")
    return MigrationResult(
        ok=True,
        mode=mode,
        user_id=user_id,
        user_name=user_name,
        source_ip=src_sn,
        target_ip=tgt_sn,
        target_user_id=dest_user_id,
        fingers_transferred=len(fingers),
        logs=logs
    )


def migrate_single_user_real(
    source_id: str | None = None,
    source_port: int = 4370,
    target_id: str | None = None,
    target_port: int = 4370,
    user_id: str = "",
    mode: str = "copy",
    timeout: int = 8,
    target_user_id: str | None = None,
    *,
    source_ip: str | None = None,
    target_ip: str | None = None,
    source_sn: str | None = None,
    target_sn: str | None = None,
) -> MigrationResult:
    """
    Perform real hardware migration between two devices over TCP 4370.
    Identified by Serial Number or IP. Zero database dependency.
    """
    src_identifier = source_id or source_sn or source_ip or "NFZ8254900401"
    tgt_identifier = target_id or target_sn or target_ip or "UFZ9876543210"

    src_res = resolve_device(src_identifier, source_port)
    tgt_res = resolve_device(tgt_identifier, target_port)
    source_sn = src_res["sn"]
    source_ip = src_res["ip"]
    source_port = src_res["port"]
    target_sn = tgt_res["sn"]
    target_ip = tgt_res["ip"]
    target_port = tgt_res["port"]

    logs = [
        f"Starting migration: User {user_id} ({mode.upper()})",
        f"Source Device: {src_res.get('name', 'Reader')} (SN: {source_sn}, IP: {source_ip}:{source_port})",
        f"Target Device: {tgt_res.get('name', 'Reader')} (SN: {target_sn}, IP: {target_ip}:{target_port})"
    ]
    if not HAS_PYZK:
        return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name="N/A",
                               source_ip=source_sn, target_ip=target_sn, target_user_id=user_id, logs=logs,
                               error="The 'pyzk' package is not installed.")

    src_conn = None
    tgt_conn = None
    user_name = f"User {user_id}"

    try:
        # 1. Connect to Source
        logs.append(f"Connecting to source device at {source_ip}:{source_port} (SN: {source_sn})...")
        _, src_conn = connect_zk_device(source_ip, source_port, timeout)
        src_conn.disable_device()
        logs.append(f"Connected to source device {source_sn}.")

        # Read source users
        src_users = src_conn.get_users()
        target_src_user = next((u for u in src_users if str(u.user_id) == str(user_id)), None)
        if not target_src_user:
            logs.append(f"[ERROR] User ID '{user_id}' not found on source device {source_ip}.")
            return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name="N/A",
                                   source_ip=source_ip, target_ip=target_ip, target_user_id=user_id, logs=logs,
                                   error=f"User ID {user_id} not found on source device.")

        user_name = target_src_user.name or f"User {user_id}"
        logs.append(f"Found source user: '{user_name}' (PIN: {user_id}, Role: {target_src_user.privilege}, Card: {target_src_user.card})")

        # Read templates for this user
        logs.append(f"Extracting enrolled fingerprint templates for {user_name} (UID: {target_src_user.uid})...")
        user_fingers: list[Any] = []
        try:
            all_templates = src_conn.get_templates()
            user_fingers = [t for t in all_templates if t.uid == target_src_user.uid]
        except Exception as e:
            logs.append(f"Bulk template read warning: {e}. Falling back to individual template scan...")
            user_fingers = []

        # Fallback to get_user_template for FIDs 0..9 if bulk was empty
        if not user_fingers:
            for fid in range(10):
                try:
                    f = src_conn.get_user_template(uid=target_src_user.uid, temp_id=fid, user_id=str(user_id))
                    if f and f.template:
                        user_fingers.append(f)
                except Exception:
                    pass

        logs.append(f"Extracted {len(user_fingers)} fingerprint template(s): FIDs {[f.fid for f in user_fingers]}")

        # 2. Connect to Target
        logs.append(f"Connecting to target device at {target_ip}:{target_port}...")
        _, tgt_conn = connect_zk_device(target_ip, target_port, timeout)
        tgt_conn.disable_device()
        logs.append("Connected to target device.")

        # Check algorithm compatibility
        try:
            src_ver = src_conn.get_fp_version()
            tgt_ver = tgt_conn.get_fp_version()
            logs.append(f"Biometric Algorithm check: Source ZKFP={src_ver}, Target ZKFP={tgt_ver}")
            if src_ver != tgt_ver:
                logs.append(f"[WARNING] FP algorithm versions differ ({src_ver} vs {tgt_ver}). Reader verification may require template format compatibility.")
        except Exception as ex:
            logs.append(f"Algorithm version check skipped ({ex})")

        # Check target user and resolve PIN
        tgt_users = tgt_conn.get_users()
        dest_user_id = str(target_user_id).strip() if target_user_id else str(user_id)
        existing_tgt_user = next((u for u in tgt_users if str(u.user_id) == dest_user_id), None)

        if existing_tgt_user and existing_tgt_user.name and existing_tgt_user.name.strip().lower() != user_name.strip().lower():
            used_pins: set[int] = set()
            for u in tgt_users:
                try:
                    used_pins.add(int(u.user_id))
                except (ValueError, TypeError):
                    pass
            cand = int(dest_user_id) if dest_user_id.isdigit() else 1
            while cand in used_pins:
                cand += 1
            dest_user_id = str(cand)
            logs.append(f"[PIN MAPPED] Target PIN '{user_id}' was occupied on {target_sn} by '{existing_tgt_user.name}'. Auto-mapped to free PIN '{dest_user_id}'.")
            existing_tgt_user = None
        elif existing_tgt_user:
            logs.append(f"Target PIN '{dest_user_id}' matches existing user '{existing_tgt_user.name}' on {target_sn}. Updating profile.")
        else:
            logs.append(f"Target PIN '{dest_user_id}' is available on {target_sn}.")

        if existing_tgt_user:
            target_uid = existing_tgt_user.uid
            logs.append(f"User {dest_user_id} already exists on target with sequence UID {target_uid}. Updating profile.")
        else:
            used_uids = {u.uid for u in tgt_users}
            target_uid = 1
            while target_uid in used_uids:
                target_uid += 1
            logs.append(f"Assigning new target sequence UID: {target_uid} (PIN: {dest_user_id})")

        # 3. Write User Profile on Target
        logs.append(f"Writing user profile on target device (PIN: {dest_user_id}, Name: '{user_name}')...")
        tgt_conn.set_user(
            uid=target_uid,
            name=user_name,
            privilege=target_src_user.privilege,
            password=str(target_src_user.password or ''),
            group_id=str(target_src_user.group_id or ''),
            user_id=str(dest_user_id),
            card=int(target_src_user.card or 0)
        )

        # 4. Write Fingerprint Templates on Target
        if user_fingers:
            logs.append(f"Writing {len(user_fingers)} fingerprint template(s) to target device...")
            target_fingers = []
            for f in user_fingers:
                target_f = Finger(
                    uid=target_uid,
                    fid=f.fid,
                    valid=f.valid,
                    template=f.template
                )
                target_fingers.append(target_f)

            target_user_obj = User(
                uid=target_uid,
                name=user_name,
                privilege=target_src_user.privilege,
                password=str(target_src_user.password or ''),
                group_id=str(target_src_user.group_id or ''),
                user_id=str(dest_user_id),
                card=int(target_src_user.card or 0)
            )
            tgt_conn.save_user_template(target_user_obj, target_fingers)
            logs.append("Fingerprint templates written successfully.")
        else:
            logs.append("No fingerprint templates to write (user is PIN/Card only).")

        tgt_conn.refresh_data()
        logs.append("Target device data refreshed.")

        # 5. Verify on Target Device
        logs.append("Verifying user presence on target device...")
        verified_tgt_users = tgt_conn.get_users()
        if not any(str(u.user_id) == str(dest_user_id) for u in verified_tgt_users):
            logs.append(f"[ERROR] Target verification failed: User {dest_user_id} not found after write.")
            return MigrationResult(ok=False, mode=mode, user_id=user_id, user_name=user_name,
                                   source_ip=source_ip, target_ip=target_ip, target_user_id=dest_user_id, logs=logs,
                                   error="Verification failed on target device")

        logs.append("Target verification PASSED: Employee record confirmed on target.")

        # 6. If MOVE mode: Delete from Source Device
        if mode.lower() == "move":
            logs.append(f"Move mode active: Deleting user {user_id} (UID: {target_src_user.uid}) from source device...")
            src_conn.delete_user(uid=target_src_user.uid, user_id=str(user_id))
            src_conn.refresh_data()
            logs.append(f"User {user_id} deleted from source device and source refreshed.")

        logs.append(f"All operations completed successfully: {mode.upper()} of {user_name} (ID: {user_id} -> Target PIN: {dest_user_id}).")
        return MigrationResult(
            ok=True,
            mode=mode,
            user_id=user_id,
            user_name=user_name,
            source_ip=source_ip,
            target_ip=target_ip,
            target_user_id=dest_user_id,
            fingers_transferred=len(user_fingers),
            logs=logs
        )

    except Exception as e:
        logger.exception("Migration failed: %s", e)
        logs.append(f"[EXCEPTION] Migration failed: {e}")
        return MigrationResult(
            ok=False,
            mode=mode,
            user_id=user_id,
            user_name=user_name,
            source_ip=source_ip,
            target_ip=target_ip,
            target_user_id=user_id,
            logs=logs,
            error=str(e)
        )
    finally:
        if src_conn:
            try:
                src_conn.enable_device()
            except Exception:
                pass
            try:
                src_conn.disconnect()
            except Exception:
                pass
        if tgt_conn:
            try:
                tgt_conn.enable_device()
            except Exception:
                pass
            try:
                tgt_conn.disconnect()
            except Exception:
                pass


def run_migration(
    source_ip: str,
    target_ip: str,
    user_ids: list[str],
    source_port: int = 4370,
    target_port: int = 4370,
    mode: str = "copy",
    simulate: bool = False,
    timeout: int = 8,
    target_user_ids: dict[str, str] | None = None,
    *,
    source_sn: str | None = None,
    target_sn: str | None = None,
    source_id: str | None = None,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Orchestrate migration for one or multiple users by Serial Number or IP.
    Zero database dependency.
    """
    results: list[dict[str, Any]] = []
    src_res = resolve_device(source_ip, source_port)
    tgt_res = resolve_device(target_ip, target_port)
    src_sn = src_res["sn"]
    tgt_sn = tgt_res["sn"]

    is_sim = simulate
    tgt_map = target_user_ids or {}

    for uid in user_ids:
        uid = str(uid).strip()
        if not uid:
            continue
        tgt_pin = tgt_map.get(uid)
        if is_sim:
            res = migrate_single_user_simulated(src_sn, tgt_sn, uid, mode, target_user_id=tgt_pin)
        else:
            res = migrate_single_user_real(src_sn, source_port, tgt_sn, target_port, uid, mode, timeout, target_user_id=tgt_pin)
        results.append(res.to_dict())

    return results

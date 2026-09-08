import logging

logger = logging.getLogger(__name__)
"""
Migration API Router.

Endpoints for testing and executing device-to-device user and biometric fingerprint
migration without database dependency.
"""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.device_migration import (
    delete_batch_device_users,
    delete_device_user,
    fetch_device_users,
    get_registered_devices,
    probe_device_connection,
    reset_simulation_state,
    resolve_target_pin,
    run_migration,
)

router = APIRouter(prefix="/api/migration", tags=["migration"])


@router.get("/devices")
async def api_get_devices() -> JSONResponse:
    """List all registered and detected biometric devices by Serial Number."""
    devices = get_registered_devices()
    dev_list = []
    for sn, info in devices.items():
        dev_list.append({
            "sn": sn,
            "name": info.get("name", f"Reader ({sn})"),
            "ip": info.get("ip", ""),
            "port": info.get("port", 4370),
            "lastSeen": info.get("lastSeen")
        })
    return JSONResponse(status_code=200, content={"count": len(dev_list), "devices": dev_list})


@router.post("/test-connection")
async def api_test_connection(request: Request) -> JSONResponse:
    """Test TCP connection to a device by Serial Number or IP and return status/specs."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body"})

    device_id = str(data.get("sn") or data.get("ip") or "").strip()
    if not device_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Device Serial Number or IP is required"})

    port = int(data.get("port", 4370))
    timeout = int(data.get("timeout", 5))
    simulate = bool(data.get("simulate", False))

    res = await asyncio.to_thread(
        probe_device_connection,
        device_id=device_id,
        port=port,
        timeout=timeout,
        simulate=simulate
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/users")
async def api_fetch_users(request: Request) -> JSONResponse:
    """Scan and list all users and their enrolled finger count from a device by Serial Number or IP."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body"})

    device_id = str(data.get("sn") or data.get("ip") or "").strip()
    if not device_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Device Serial Number or IP is required"})

    port = int(data.get("port", 4370))
    timeout = int(data.get("timeout", 5))
    simulate = bool(data.get("simulate", False))

    res = await asyncio.to_thread(
        fetch_device_users,
        device_id=device_id,
        port=port,
        timeout=timeout,
        simulate=simulate
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/run")
async def api_run_migration(request: Request) -> JSONResponse:
    """Execute device-to-device migration for specified user(s) using Serial Numbers or IPs."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body"})

    source_id = str(data.get("sourceSn") or data.get("sourceIp") or "").strip()
    target_id = str(data.get("targetSn") or data.get("targetIp") or "").strip()
    source_port = int(data.get("sourcePort", 4370))
    target_port = int(data.get("targetPort", 4370))
    user_ids = data.get("userIds", [])
    mode = str(data.get("mode", "copy")).lower().strip()
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 8))

    if not source_id or not target_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Both Source and Target Serial Numbers (or IPs) are required"})

    if source_id == target_id and source_port == target_port:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Source and Target cannot be the exact same device and port"})

    if not user_ids:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Please select or provide at least one User ID to migrate"})

    if isinstance(user_ids, str):
        user_ids = [u.strip() for u in user_ids.split(",") if u.strip()]

    results = await asyncio.to_thread(
        run_migration,
        source_ip=source_id,
        source_port=source_port,
        target_ip=target_id,
        target_port=target_port,
        user_ids=user_ids,
        mode=mode,
        simulate=simulate,
        timeout=timeout
    )

    all_ok = all(r.get("ok", False) for r in results)
    summary = f"Successfully transferred {sum(r.get('fingersTransferred', 0) for r in results)} templates across {len(results)} employees."

    return JSONResponse(status_code=200 if all_ok else 207, content={
        "ok": all_ok,
        "summary": summary,
        "results": results
    })


@router.post("/delete")
async def api_delete_device_user(request: Request) -> JSONResponse:
    """Delete a user (or multiple users) and enrolled biometric templates from a device by Serial Number or IP."""
    try:
        data = await request.json()
    except Exception as exc:
        logger.error("Invalid JSON body in /api/migration/delete: %s", exc)
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "Invalid JSON body"})

    device_id = str(
        data.get("sn")
        or data.get("device_sn")
        or data.get("deviceSn")
        or data.get("target_sn")
        or data.get("targetSn")
        or data.get("source_sn")
        or data.get("sourceSn")
        or data.get("deviceId")
        or data.get("device_id")
        or data.get("ip")
        or ""
    ).strip()

    user_id = str(
        data.get("userId")
        or data.get("user_id")
        or data.get("pin")
        or data.get("user_pin")
        or data.get("targetPin")
        or data.get("target_pin")
        or ""
    ).strip()

    user_ids = data.get("userIds") or data.get("user_ids") or data.get("pins") or data.get("users")
    delete_biometrics_only = bool(data.get("deleteBiometricsOnly") or data.get("delete_biometrics_only") or False)

    logger.info("POST /api/migration/delete -> device_id: '%s', user_id: '%s', user_ids: %s, biometrics_only: %s",
                device_id, user_id, user_ids, delete_biometrics_only)

    if not device_id:
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "Device Serial Number or IP is required (keys: sn, device_sn, deviceId, ip)"})

    port = int(data.get("port", 4370))
    timeout = int(data.get("timeout", 8))
    simulate = bool(data.get("simulate", False))
    ip_override = str(data.get("ip", "")).strip() or None

    # If batch list of user IDs provided
    if user_ids and isinstance(user_ids, list):
        res = await asyncio.to_thread(
            delete_batch_device_users,
            device_id=device_id,
            user_ids=[str(u) for u in user_ids],
            port=port,
            timeout=timeout,
            delete_biometrics_only=delete_biometrics_only,
            simulate=simulate,
            ip_override=ip_override
        )
        res["success"] = res.get("ok", False)
        status_code = 200 if res.get("ok") else 400
        return JSONResponse(status_code=status_code, content=res)

    if not user_id:
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "User ID / PIN or userIds array is required (keys: userId, user_id, pin)"})

    res = await asyncio.to_thread(
        delete_device_user,
        device_id=device_id,
        user_id=user_id,
        port=port,
        timeout=timeout,
        delete_biometrics_only=delete_biometrics_only,
        simulate=simulate,
        ip_override=ip_override
    )
    res["success"] = res.get("ok", False)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/delete-batch")
async def api_delete_batch_device_users(request: Request) -> JSONResponse:
    """Batch delete multiple users and/or their biometric fingerprints from a device."""
    try:
        data = await request.json()
    except Exception as exc:
        logger.error("Invalid JSON body in /api/migration/delete-batch: %s", exc)
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "Invalid JSON body"})

    device_id = str(
        data.get("sn")
        or data.get("device_sn")
        or data.get("deviceSn")
        or data.get("target_sn")
        or data.get("targetSn")
        or data.get("source_sn")
        or data.get("sourceSn")
        or data.get("deviceId")
        or data.get("device_id")
        or data.get("ip")
        or ""
    ).strip()

    user_ids = data.get("userIds") or data.get("user_ids") or data.get("pins") or data.get("users") or []
    delete_biometrics_only = bool(data.get("deleteBiometricsOnly") or data.get("delete_biometrics_only") or False)
    port = int(data.get("port", 4370))
    timeout = int(data.get("timeout", 8))
    simulate = bool(data.get("simulate", False))
    ip_override = str(data.get("ip", "")).strip() or None

    logger.info("POST /api/migration/delete-batch -> device_id: '%s', user_ids: %s, biometrics_only: %s",
                device_id, user_ids, delete_biometrics_only)

    if not device_id:
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "Device Serial Number or IP is required"})
    if not user_ids:
        return JSONResponse(status_code=400, content={"ok": False, "success": False, "error": "userIds array is required and cannot be empty"})

    res = await asyncio.to_thread(
        delete_batch_device_users,
        device_id=device_id,
        user_ids=[str(u) for u in user_ids],
        port=port,
        timeout=timeout,
        delete_biometrics_only=delete_biometrics_only,
        simulate=simulate,
        ip_override=ip_override
    )
    res["success"] = res.get("ok", False)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/transfer-user")
async def api_transfer_single_user(request: Request) -> JSONResponse:
    """Transfer or copy a single employee's profile and biometric templates with PIN mapping."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body"})

    source_id = str(data.get("sourceSn") or data.get("sourceIp") or data.get("sourceDevice") or "").strip()
    target_id = str(data.get("targetSn") or data.get("targetIp") or data.get("targetDevice") or "").strip()
    user_id = str(data.get("userId") or data.get("user_id") or "").strip()
    target_pin = str(data.get("targetPin") or data.get("targetUserId") or "").strip() or None
    mode = str(data.get("mode", "copy")).lower().strip()
    source_port = int(data.get("sourcePort", 4370))
    target_port = int(data.get("targetPort", 4370))
    timeout = int(data.get("timeout", 8))
    simulate = bool(data.get("simulate", False))

    if not source_id or not target_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Both source and target devices are required"})
    if not user_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "User ID / PIN is required"})

    tgt_map = {user_id: target_pin} if target_pin else None
    results = await asyncio.to_thread(
        run_migration,
        source_ip=source_id,
        source_port=source_port,
        target_ip=target_id,
        target_port=target_port,
        user_ids=[user_id],
        mode=mode,
        simulate=simulate,
        timeout=timeout,
        target_user_ids=tgt_map
    )

    if not results:
        return JSONResponse(status_code=500, content={"ok": False, "error": "Migration produced no result"})

    res = results[0]
    return JSONResponse(status_code=200 if res.get("ok") else 400, content=res)


@router.post("/resolve-pin")
async def api_resolve_target_pin(request: Request) -> JSONResponse:
    """Check target device for PIN conflicts and return same or next available free PIN."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body"})

    target_id = str(data.get("targetSn") or data.get("targetIp") or data.get("deviceId") or "").strip()
    preferred_pin = str(data.get("pin") or data.get("userId") or "").strip()
    user_name = str(data.get("userName") or data.get("name") or "").strip() or None
    port = int(data.get("port", 4370))
    timeout = int(data.get("timeout", 5))
    simulate = bool(data.get("simulate", False))

    if not target_id or not preferred_pin:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Target device and preferred PIN are required"})

    res = await asyncio.to_thread(
        resolve_target_pin,
        target_id=target_id,
        preferred_pin=preferred_pin,
        user_name=user_name,
        port=port,
        timeout=timeout,
        simulate=simulate
    )
    return JSONResponse(status_code=200 if res.get("ok") else 400, content=res)


@router.post("/reset-sim")
async def api_reset_sim() -> JSONResponse:
    """Reset simulated test devices back to initial state using real employee records."""
    reset_simulation_state()
    return JSONResponse(status_code=200, content={"ok": True, "message": "Simulated devices reset to default state with real employee records."})

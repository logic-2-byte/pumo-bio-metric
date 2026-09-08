"""
Employee & Multi-Branch Biometric API Router.

Endpoints for managing employees, branch transfers with automatic biometric
data migration (Move) and multi-branch punch access (Copy), and physical
device biometric deletion (Revoke).
"""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.employee_service import (
    delete_employee_permanently,
    fire_employee_and_wipe_biometrics,
    get_branches,
    get_employee_by_id,
    grant_multi_branch_access,
    load_employees,
    revoke_employee_device_access,
    transfer_employee,
)

router = APIRouter(prefix="/api/employees", tags=["employees"])


@router.get("")
async def api_list_employees() -> JSONResponse:
    """List all employees, their current branch, and active biometric device access."""
    employees = load_employees()
    branches = {b["id"]: b for b in get_branches()}

    enriched = []
    for emp in employees:
        branch = branches.get(emp.get("branchId"))
        enriched.append({
            **emp,
            "branchName": branch["name"] if branch else f"Branch {emp.get('branchId')}",
            "branchCode": branch["code"] if branch else f"BR-{emp.get('branchId')}",
        })
    return JSONResponse(status_code=200, content={"count": len(enriched), "employees": enriched})


@router.get("/branches")
async def api_list_branches() -> JSONResponse:
    """List all branches and their registered biometric readers."""
    branches = get_branches()
    return JSONResponse(status_code=200, content={"count": len(branches), "branches": branches})


@router.get("/{employee_id}")
async def api_get_employee(employee_id: str) -> JSONResponse:
    """Get single employee details by ID or employee code."""
    emp = get_employee_by_id(employee_id)
    if not emp:
        return JSONResponse(status_code=404, content={"ok": False, "error": f"Employee {employee_id} not found."})
    return JSONResponse(status_code=200, content={"ok": True, "employee": emp})


@router.post("/transfer")
async def api_transfer_employee(request: Request) -> JSONResponse:
    """
    Transfer an employee to a different branch.
    By default (transferBiometric=True), automatically transfers their biometric
    profile and enrolled fingerprint templates:
    - Mode 'move': verifies on destination reader and deletes from current reader.
    - Mode 'copy': keeps current branch access and copies to destination reader (multi-branch access).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

    emp_id = data.get("employeeId") or data.get("id")
    to_branch_id = data.get("toBranchId")
    if not emp_id or to_branch_id is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Both employeeId and toBranchId are required"})

    try:
        to_branch_id = int(to_branch_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "error": "toBranchId must be an integer"})

    effective_date = data.get("effectiveDate")
    reason = data.get("reason")
    transfer_biometric = bool(data.get("transferBiometric", True))
    biometric_mode = str(data.get("biometricMode", "move")).lower().strip()
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 8))

    res = await asyncio.to_thread(
        transfer_employee,
        employee_id=emp_id,
        to_branch_id=to_branch_id,
        effective_date=effective_date,
        reason=reason,
        transfer_biometric=transfer_biometric,
        biometric_mode=biometric_mode,
        simulate=simulate,
        timeout=timeout
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/grant-access")
async def api_grant_access(request: Request) -> JSONResponse:
    """
    Grant an employee biometric access to another branch reader without changing
    their home branch (Copy Mode / Roaming Access).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

    emp_id = data.get("employeeId") or data.get("id")
    target_branch_id = data.get("targetBranchId")
    target_device_sn = data.get("targetDeviceSn") or data.get("sn")
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 8))

    if not emp_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "employeeId is required"})
    if not target_branch_id and not target_device_sn:
        return JSONResponse(status_code=400, content={"ok": False, "error": "targetBranchId or targetDeviceSn is required"})

    if target_branch_id:
        try:
            target_branch_id = int(target_branch_id)
        except ValueError:
            pass

    res = await asyncio.to_thread(
        grant_multi_branch_access,
        employee_id=emp_id,
        target_branch_id=target_branch_id,
        target_device_sn=target_device_sn,
        simulate=simulate,
        timeout=timeout
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/revoke-access")
async def api_revoke_access(request: Request) -> JSONResponse:
    """
    Revoke biometric access by deleting the employee's fingerprints & user profile
    from a specific hardware reader (e.g. revoking a copied reader access).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

    emp_id = data.get("employeeId") or data.get("id")
    device_sn = data.get("deviceSn") or data.get("sn")
    delete_biometrics_only = bool(data.get("deleteBiometricsOnly", False))
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 5))

    if not emp_id or not device_sn:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Both employeeId and deviceSn are required"})

    res = await asyncio.to_thread(
        revoke_employee_device_access,
        employee_id=emp_id,
        device_sn=device_sn,
        delete_biometrics_only=delete_biometrics_only,
        simulate=simulate,
        timeout=timeout
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/offboard")
@router.post("/fire")
async def api_offboard_employee(request: Request) -> JSONResponse:
    """
    Dedicated Offboarding & Biometric Wiping:
    Permanently revokes and deletes an employee's biometric fingerprints across
    ALL enrolled hardware readers (when fired/dismissed) or from a selected reader.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

    emp_id = data.get("employeeId") or data.get("id")
    action = str(data.get("action", "fire_all")).lower().strip()
    device_sn = data.get("deviceSn") or data.get("sn")
    delete_mode = str(data.get("deleteMode", "full")).lower().strip()
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 5))

    if not emp_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "employeeId is required"})

    res = await asyncio.to_thread(
        fire_employee_and_wipe_biometrics,
        employee_id=emp_id,
        action=action,
        device_sn=device_sn,
        delete_mode=delete_mode,
        simulate=simulate,
        timeout=timeout
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)


@router.post("/delete")
async def api_delete_employee(request: Request) -> JSONResponse:
    """
    Delete an employee record completely and wipe biometric templates from all readers.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

    emp_id = data.get("employeeId") or data.get("id")
    wipe_biometrics = bool(data.get("wipeBiometrics", True))
    simulate = bool(data.get("simulate", False))
    timeout = int(data.get("timeout", 5))

    if not emp_id:
        return JSONResponse(status_code=400, content={"ok": False, "error": "employeeId is required"})

    res = await asyncio.to_thread(
        delete_employee_permanently,
        employee_id=emp_id,
        wipe_biometrics=wipe_biometrics,
        simulate=simulate,
        timeout=timeout
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(status_code=status_code, content=res)

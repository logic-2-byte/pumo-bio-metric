"""
Device-to-device user transfer over ADMS, for readers this server cannot dial.

The TCP path (`migrate_single_user_real`) needs a socket to port 4370 on both
readers, which a cloud host never has — every transfer timed out. Over ADMS a
transfer is a short job spread across a few polls:

  1. Ask the source for its user table. Its answer carries the profile as a
     USER line and every fingerprint as an FP line with the template in TMP=.
  2. When the source acknowledges that query, its answer is complete: write the
     profile and templates to the target.
  3. For a move, delete from the source only after the target acknowledged
     every write, so a failed copy never loses anybody.

Templates live only inside a job, and only until they are queued for the
target. They are never logged (see `redact_command`).
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from typing import Any
from app.core import clock

TRANSFER_JOBS: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def redact_command(command: str) -> str:
    """Command text safe to print or show: the template payload is dropped."""
    return re.sub(r"\bTMP=\S+", "TMP=<omitted>", command, flags=re.IGNORECASE)


def _command_id(cmd_str: str) -> str:
    parts = cmd_str.split(":", 2)
    return parts[1] if len(parts) >= 2 else ""


def _now() -> str:
    return clock.now().isoformat()


def _waiting_jobs(sn: str, pin: str) -> list[dict[str, Any]]:
    return [
        job for job in TRANSFER_JOBS.values()
        if job["status"] == "waiting_source" and job["sourceSn"] == sn and job["pin"] == pin
    ]


def start_transfer(source_sn: str, target_sn: str, pin: str, mode: str,
                   target_pin: str | None = None) -> dict[str, Any]:
    """Queue a transfer and return at once; the readers finish it on their next polls."""
    from app.core.device_migration import resolve_target_pin
    from app.router.iclock import get_user_info, queue_device_cmd

    src = str(source_sn).strip().upper()
    tgt = str(target_sn).strip().upper()
    pin = str(pin).strip()
    mode = "move" if str(mode).lower() == "move" else "copy"
    name = get_user_info(pin, src).get("name", "")
    known_name = "" if name.startswith("User ") else name

    if not target_pin:
        target_pin = resolve_target_pin(tgt, pin, known_name or None).get("targetPin") or pin

    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "id": job_id, "sourceSn": src, "targetSn": tgt, "pin": pin,
        "targetPin": str(target_pin), "mode": mode, "status": "waiting_source",
        "name": known_name, "profile": None, "templates": {},
        "sourceCommandId": "", "targetCommandIds": [], "pendingTargetIds": [],
        "fingersTransferred": 0, "error": None,
        "createdAt": _now(), "updatedAt": _now(),
    }
    with _lock:
        TRANSFER_JOBS[job_id] = job
        cmd = queue_device_cmd(src, "DATA QUERY USERINFO", action="transfer-source")
        job["sourceCommandId"] = _command_id(cmd)

    message = (f"Transfer queued: reading PIN {pin} from {src}, then writing it to {tgt} "
               f"as PIN {target_pin}. It completes over the next few device polls.")
    print(f"\033[1;36m[ADMS Transfer {job_id}]\033[0m {mode.upper()} PIN {pin} {src} -> {tgt} (PIN {target_pin}) queued")
    return {
        "ok": True, "queued": True, "jobId": job_id, "mode": mode,
        "userId": pin, "targetUserId": str(target_pin), "userName": known_name, "name": known_name,
        "sourceIp": src, "targetIp": tgt, "fingersTransferred": 0,
        "logs": [message], "message": message, "error": None,
    }


def capture_profile(sn: str, pin: str, name: str, privilege: int, card: str) -> None:
    """A USER line from a source answer: remember the profile for jobs waiting on it."""
    if not TRANSFER_JOBS:
        return
    with _lock:
        for job in _waiting_jobs(str(sn).strip().upper(), str(pin).strip()):
            job["profile"] = {"name": name, "privilege": privilege, "card": card}


def capture_template(sn: str, pin: str, line: str) -> None:
    """An FP line from a source answer: keep the template for jobs waiting on it."""
    if not TRANSFER_JOBS:
        return
    with _lock:
        jobs = _waiting_jobs(str(sn).strip().upper(), str(pin).strip())
        if not jobs:
            return
        # Whitespace split, not a regex over the line: base64 has no spaces but
        # can contain "/FID=" or "+Size=", which a pattern would mistake for fields.
        fields = {}
        for token in line.split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key.upper()] = value
        tmp = fields.get("TMP", "")
        if not tmp or "FID" not in fields:
            return
        template = {
            "size": fields.get("SIZE") or str(len(tmp)),
            "valid": fields.get("VALID") or "1",
            "tmp": tmp,
        }
        for job in jobs:
            job["templates"][fields["FID"]] = template


def on_command_result(command_id: str, return_code: str) -> None:
    """Advance any job waiting on this acknowledgement."""
    if not TRANSFER_JOBS or not command_id:
        return
    ok = return_code == "0"
    with _lock:
        for job in list(TRANSFER_JOBS.values()):
            if job["status"] == "waiting_source" and job["sourceCommandId"] == command_id:
                if ok:
                    _write_target(job)
                else:
                    _fail(job, f"Source reader refused the user query (code {return_code or 'none'})")
            elif job["status"] == "writing_target" and command_id in job["pendingTargetIds"]:
                if not ok:
                    _fail(job, f"Target reader refused a write (code {return_code or 'none'}); "
                               "the source was left untouched")
                    continue
                job["pendingTargetIds"].remove(command_id)
                if not job["pendingTargetIds"]:
                    _finish(job)


def _fail(job: dict[str, Any], error: str) -> None:
    job["status"] = "failed"
    job["error"] = error
    job["templates"] = {}
    job["updatedAt"] = _now()
    print(f"\033[1;31m[ADMS Transfer {job['id']}]\033[0m failed: {error}")


def _write_target(job: dict[str, Any]) -> None:
    from app.router.iclock import queue_device_cmd

    profile = job["profile"]
    if profile is None and not job["templates"]:
        _fail(job, f"PIN {job['pin']} was not in {job['sourceSn']}'s answer — it is not on that reader")
        return
    profile = profile or {"name": job["name"], "privilege": 0, "card": ""}
    name = profile.get("name") or job["name"] or ""
    privilege = 14 if int(profile.get("privilege") or 0) == 14 else 0
    card = str(profile.get("card") or "")
    tpin = job["targetPin"]

    ids = [_command_id(queue_device_cmd(
        job["targetSn"],
        f"DATA UPDATE USERINFO PIN={tpin}\tName={name}\tPri={privilege}\tPasswd=\tCard={card}\tGrp=1",
        action="transfer-target",
    ))]
    for fid, template in sorted(job["templates"].items()):
        ids.append(_command_id(queue_device_cmd(
            job["targetSn"],
            f"DATA UPDATE FINGERTMP PIN={tpin}\tFID={fid}\tSize={template['size']}"
            f"\tValid={template['valid']}\tTMP={template['tmp']}",
            action="transfer-target",
        )))

    job["name"] = name
    job["fingersTransferred"] = len(job["templates"])
    job["templates"] = {}
    job["targetCommandIds"] = ids
    job["pendingTargetIds"] = list(ids)
    job["status"] = "writing_target"
    job["updatedAt"] = _now()
    print(f"\033[1;36m[ADMS Transfer {job['id']}]\033[0m source answered — writing '{name}' "
          f"with {job['fingersTransferred']} fingerprint(s) to {job['targetSn']}")


def _finish(job: dict[str, Any]) -> None:
    from app.core.device_migration import _queue_delete_user_commands, _remove_user_metadata
    from app.router.iclock import save_device_user_db

    role = "Super Admin" if (job["profile"] or {}).get("privilege") == 14 else "Normal User"
    save_device_user_db(job["targetSn"], job["targetPin"], job["name"] or f"User {job['targetPin']}", role)
    if job["mode"] == "move":
        _queue_delete_user_commands(job["sourceSn"], job["pin"], delete_biometrics_only=False)
        _remove_user_metadata(job["sourceSn"], job["pin"])
    job["status"] = "completed"
    job["updatedAt"] = _now()
    print(f"\033[1;32m[ADMS Transfer {job['id']}]\033[0m {job['targetSn']} confirmed every write"
          + (f"; removal from {job['sourceSn']} queued" if job["mode"] == "move" else ""))


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        jobs = [{k: v for k, v in job.items() if k != "templates"} for job in TRANSFER_JOBS.values()]
    return sorted(jobs, key=lambda j: j["createdAt"], reverse=True)

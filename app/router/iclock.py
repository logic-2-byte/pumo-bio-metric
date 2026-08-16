import asyncio
import json
import os
import secrets
import socket
import time
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

router = APIRouter(tags=["iclock"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
LOGS_FILE = os.path.join(BASE_DIR, "attendance_logs.json")
USER_NAMES_FILE = os.path.join(BASE_DIR, "user_names.json")

# In-memory punch records store
PUNCH_LOGS: list[dict] = []
if os.path.exists(LOGS_FILE):
    try:
        with open(LOGS_FILE, encoding="utf-8") as f:
            PUNCH_LOGS = json.load(f)
    except Exception:
        PUNCH_LOGS = []

# Persistent custom user names store
DEVICE_USER_CACHE: dict[str, dict] = {
    "1": {"name": "Admin / User 1", "role": "Super Admin"},
    "2": {"name": "User 2", "role": "Normal User"},
    "101": {"name": "Member 101", "role": "Normal User"},
    "102": {"name": "Member 102", "role": "Normal User"},
    "111": {"name": "ARULAJAY", "role": "Super Admin"},
    "8888": {"name": "Device Manager", "role": "Manager"},
    "9999": {"name": "Master Super Admin", "role": "Super Admin"},
}

if os.path.exists(USER_NAMES_FILE):
    try:
        with open(USER_NAMES_FILE, encoding="utf-8") as f:
            saved_names = json.load(f)
            for uid, info in saved_names.items():
                if isinstance(info, str):
                    DEVICE_USER_CACHE[str(uid)] = {"name": info, "role": "Normal User"}
                elif isinstance(info, dict):
                    DEVICE_USER_CACHE[str(uid)] = info
    except Exception:
        pass


def save_user_cache():
    try:
        with open(USER_NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(DEVICE_USER_CACHE, f, indent=2)
    except Exception as e:
        print(f"Error saving user names file: {e}")


# SSE subscriber queues for real-time web dashboard
SSE_SUBSCRIBERS: list[asyncio.Queue] = []

# Global cache for device sync times, for this service's own dashboard.
LAST_SYNC_TIMES: dict[str, float] = {}


def mark_seen(sn: str | None, request: Request | None = None, *, force: bool = False) -> None:
    """
    Record that a reader just called in — here and in the LMS.

    THIS IS THE WHOLE OF LIVENESS NOW. Nothing probes a reader: it is on a
    branch LAN behind a router this service cannot route to, so a probe would
    fail on a healthy device and cost egress to say so. But the reader calls
    *us* every few seconds whether or not anybody punched, and every one of
    those requests is the device proving it is up, for free.

    So every iClock handler calls this. The in-memory map feeds this service's
    own dashboard; `app.sync.liveness` writes the LMS column the console's
    online dot reads, throttled and off the request path.
    """
    if not sn:
        return
    LAST_SYNC_TIMES[sn] = time.time()

    client_ip = None
    if request is not None and request.client:
        client_ip = request.client.host

    # Imported here rather than at module import so this router still loads on
    # a host without the sync package's dependencies — the local dashboard is
    # expected to work standalone.
    try:
        from app.sync.liveness import mark_seen as record_contact

        record_contact(sn, ip_address=client_ip, force=force)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"\033[1;33m[Liveness]\033[0m {sn}: not recorded ({exc})", flush=True)


# Verification type human-readable mapping
VERIFY_MODES = {
    "0": "Password / PIN",
    "1": "Fingerprint",
    "2": "RFID Card",
    "3": "Password",
    "4": "Face Recognition",
    "15": "Face / Palm",
    "255": "Standard Biometric",
}

# Attendance state human-readable mapping
PUNCH_STATUS_MAP = {
    "0": "Check-In",
    "1": "Check-Out",
    "2": "Break-Out",
    "3": "Break-In",
    "4": "Overtime-In",
    "5": "Overtime-Out",
    "255": "Present (Auto Attendance)",
}


def get_local_ips() -> list[str]:
    """
    Local IPv4 addresses to show in the console and dashboard, best first.

    "Best" matters because the first one is what an operator types into a
    reader. A dev box carries virtual adapters — WSL, Hyper-V, Docker — whose
    addresses hostname resolution happily returns first, and those subnets
    exist only inside this host: a reader pointed at one can never reach us.

    So the address the machine actually routes off itself with is asked for
    directly (the UDP connect picks a route without sending a packet) and put
    at the front; the rest follow for reference, link-local last.
    """
    ips: list[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            ips.append(probe.getsockname()[0])
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        others = [ip for ip in socket.gethostbyname_ex(hostname)[2] if not ip.startswith("127.")]
        # 169.254.x is APIPA — an adapter that never got a lease. Never useful.
        others.sort(key=lambda ip: ip.startswith("169.254."))
        ips.extend(ip for ip in others if ip not in ips)
    except Exception:
        pass

    return ips if ips else ["127.0.0.1"]


def get_user_info(user_id: str) -> dict:
    """Helper: Get user info from cache or return default."""
    u_id = str(user_id).strip()
    if u_id in DEVICE_USER_CACHE:
        return DEVICE_USER_CACHE[u_id]
    return {"name": f"User {u_id}", "role": "Normal User"}


def format_punch_banner(punch: dict, client_ip: str = "") -> str:
    """Helper: Format a high-visibility terminal banner with user name, role, and exact status."""
    user_id = str(punch.get("userId", "UNKNOWN")).strip()
    ts = punch.get("timestamp", "N/A")
    sn = punch.get("sn", "UNKNOWN")

    status_code = str(punch.get("status", "0"))
    status_label = PUNCH_STATUS_MAP.get(status_code, f"Status ({status_code})")

    verify_code = str(punch.get("verifyType", "1"))
    verify_label = VERIFY_MODES.get(verify_code, f"Type ({verify_code})")

    user_info = get_user_info(user_id)
    user_name = punch.get("userName") or user_info.get("name", f"User {user_id}")
    user_role = punch.get("userRole") or user_info.get("role", "Normal User")

    ip_info = f" | IP: {client_ip}" if client_ip else ""
    return (
        f"\n\033[1;92m╔════════════════════════════════════════════════════════════════════════════╗\033[0m\n"
        f"\033[1;92m║ 🔔 LIVE BIOMETRIC PUNCH CAPTURED                                           ║\033[0m\n"
        f"\033[1;92m╠════════════════════════════════════════════════════════════════════════════╣\033[0m\n"
        f"  \033[1;33m👤 User Name       :\033[0m \033[1;97m{user_name:<25}\033[0m\n"
        f"  \033[1;36m🆔 User ID / PIN   :\033[0m \033[1;97m{user_id:<25}\033[0m\n"
        f"  \033[1;35m🛡️ Role / Type     :\033[0m \033[1;95m{user_role:<25}\033[0m\n"
        f"  \033[1;36m🕒 Punch Time      :\033[0m \033[1;97m{ts:<25}\033[0m\n"
        f"  \033[1;32m📌 Attendance State:\033[0m \033[1;92m{status_label:<25}\033[0m (Code: {status_code})\n"
        f"  \033[1;35m🔍 Verification    :\033[0m \033[1;95m{verify_label:<25}\033[0m (Code: {verify_code})\n"
        f"  \033[1;34m📟 Device Serial   :\033[0m \033[1;94m{sn}\033[0m{ip_info}\n"
        f"\033[1;92m╚════════════════════════════════════════════════════════════════════════════╝\033[0m\n"
    )


def broadcast_punch(punch: dict):
    """Broadcast new punch to all connected SSE clients and persist to local JSON file."""
    user_id = str(punch.get("userId", "")).strip()
    user_info = get_user_info(user_id)

    if not punch.get("userName") or punch.get("userName", "").startswith("User "):
        if user_info.get("name") and not user_info.get("name").startswith("User "):
            punch["userName"] = user_info["name"]
        else:
            punch.setdefault("userName", user_info.get("name", f"User {user_id}"))
    punch.setdefault("userRole", user_info.get("role", "Normal User"))

    status_code = str(punch.get("status", "0"))
    punch["statusLabel"] = PUNCH_STATUS_MAP.get(status_code, f"Status {status_code}")

    verify_code = str(punch.get("verifyType", "1"))
    punch["verifyLabel"] = VERIFY_MODES.get(verify_code, f"Verify {verify_code}")

    PUNCH_LOGS.insert(0, punch)
    if len(PUNCH_LOGS) > 1000:
        del PUNCH_LOGS[1000:]

    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(PUNCH_LOGS, f, indent=2, default=str)
    except Exception as e:
        print(f"Error persisting logs to file: {e}")

    data_str = json.dumps(punch, default=str)
    for q in list(SSE_SUBSCRIBERS):
        # A subscriber whose queue is full or already closed is a browser tab
        # that has gone away. Dropping its copy is correct; the punch is
        # already persisted.
        with suppress(Exception):
            q.put_nowait(data_str)


def is_header_or_metadata_line(line: str) -> bool:
    """Check if a line is a header or metadata line rather than a real punch row."""
    l_lower = line.lower().strip()
    if not l_lower:
        return True
    return l_lower.startswith((
        "table=", "stamp=", "opstamp=", "count=", "oplog", "operlog",
        "attlog", "rtlog", "cdata", "#", "[", "table ",
    ))


def parse_attlog(body_str: str, sn: str) -> list[dict]:
    """
    Robust multi-format attendance log parser for eSSL / ZKTeco devices.
    """
    if not body_str:
        return []

    lines = [line.strip() for line in body_str.replace("\r", "\n").split("\n") if line.strip()]
    parsed_records = []

    for line in lines:
        if is_header_or_metadata_line(line):
            continue

        # 1. Key-Value format
        if "PIN=" in line.upper() or "TIME=" in line.upper():
            kv_dict = {}
            for item in line.replace("\t", " ").split():
                if "=" in item:
                    k, v = item.split("=", 1)
                    kv_dict[k.strip().upper()] = v.strip()
            pin = str(kv_dict.get("PIN") or kv_dict.get("USERID") or kv_dict.get("ID") or "")
            ts_str = kv_dict.get("TIME") or kv_dict.get("TIMESTAMP") or kv_dict.get("DATETIME")
            if pin and ts_str:
                user_info = get_user_info(pin)
                parsed_records.append({
                    "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                    "sn": sn or "UNKNOWN",
                    "userId": pin,
                    "userName": user_info.get("name", f"User {pin}"),
                    "userRole": user_info.get("role", "Normal User"),
                    "timestamp": ts_str,
                    "status": str(kv_dict.get("STATUS", "0")),
                    "verifyType": str(kv_dict.get("VERIFY", kv_dict.get("VERIFYTYPE", "1"))),
                    "workCode": str(kv_dict.get("WORKCODE", "0")),
                    "raw": line,
                    "receivedAt": datetime.now().isoformat()
                })
                continue

        # 2. Standard Tab-separated
        parts = [p.strip() for p in line.split("\t") if p.strip() or p == ""]
        if len(parts) >= 2 and parts[0]:
            pin = str(parts[0])
            ts_candidate = parts[1]
            if len(parts) >= 3 and "-" not in ts_candidate and ":" not in ts_candidate and "-" in parts[2]:
                ts_candidate = f"{parts[1]} {parts[2]}"

            status_val = parts[2] if len(parts) > 2 and parts[2] != "" else "0"
            verify_val = parts[3] if len(parts) > 3 and parts[3] != "" else "1"
            work_val = parts[4] if len(parts) > 4 and parts[4] != "" else "0"

            user_info = get_user_info(pin)
            parsed_records.append({
                "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "sn": sn or "UNKNOWN",
                "userId": pin,
                "userName": user_info.get("name", f"User {pin}"),
                "userRole": user_info.get("role", "Normal User"),
                "timestamp": ts_candidate,
                "status": str(status_val),
                "verifyType": str(verify_val),
                "workCode": str(work_val),
                "raw": line,
                "receivedAt": datetime.now().isoformat()
            })
            continue

        # 3. Space-separated
        space_parts = line.split()
        if len(space_parts) >= 3 and ("-" in space_parts[1] or "/" in space_parts[1]) and ":" in space_parts[2]:
            pin = str(space_parts[0])
            ts_str = f"{space_parts[1]} {space_parts[2]}"
            status_val = space_parts[3] if len(space_parts) > 3 else "0"
            verify_val = space_parts[4] if len(space_parts) > 4 else "1"
            work_val = space_parts[5] if len(space_parts) > 5 else "0"

            user_info = get_user_info(pin)
            parsed_records.append({
                "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "sn": sn or "UNKNOWN",
                "userId": pin,
                "userName": user_info.get("name", f"User {pin}"),
                "userRole": user_info.get("role", "Normal User"),
                "timestamp": ts_str,
                "status": str(status_val),
                "verifyType": str(verify_val),
                "workCode": str(work_val),
                "raw": line,
                "receivedAt": datetime.now().isoformat()
            })
            continue

        # 4. Comma-separated
        comma_parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(comma_parts) >= 2 and (("-" in comma_parts[1] or "/" in comma_parts[1]) or ":" in comma_parts[1]):
            pin = str(comma_parts[0])
            user_info = get_user_info(pin)
            parsed_records.append({
                "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "sn": sn or "UNKNOWN",
                "userId": pin,
                "userName": user_info.get("name", f"User {pin}"),
                "userRole": user_info.get("role", "Normal User"),
                "timestamp": comma_parts[1],
                "status": str(comma_parts[2] if len(comma_parts) > 2 else "0"),
                "verifyType": str(comma_parts[3] if len(comma_parts) > 3 else "1"),
                "workCode": str(comma_parts[4] if len(comma_parts) > 4 else "0"),
                "raw": line,
                "receivedAt": datetime.now().isoformat()
            })
            continue

        # 5. Raw Fallback
        pin = str(parts[0]) if parts else "DEVICE_USER"
        user_info = get_user_info(pin)
        parsed_records.append({
            "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
            "sn": sn or "UNKNOWN",
            "userId": pin,
            "userName": user_info.get("name", f"User {pin}"),
            "userRole": user_info.get("role", "Normal User"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "0",
            "verifyType": "1",
            "workCode": "0",
            "raw": line,
            "receivedAt": datetime.now().isoformat()
        })

    return parsed_records


def parse_oplog(body_str: str, sn: str):
    """Parse and log device administration operations (OPLOG / OPERLOG)."""
    if not body_str:
        return
    lines = [line.strip() for line in body_str.replace("\r", "\n").split("\n") if line.strip()]
    for line in lines:
        if is_header_or_metadata_line(line):
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            operator_pin = str(parts[0]).strip()
            op_type = parts[1]
            op_time = parts[2]
            user_info = get_user_info(operator_pin)
            op_name = user_info.get("name", f"Admin {operator_pin}")
            print(f"\033[1;35m[iClock Admin Action]\033[0m Device: {sn} | Admin: {op_name} (ID: {operator_pin}) | OpType: {op_type} | Time: {op_time}")
        else:
            print(f"\033[1;35m[iClock Admin Action]\033[0m Device: {sn} | Raw: {line}")


def get_query_param_ci(request: Request, key: str, default: str | None = None) -> str | None:
    """Case-insensitive query parameter lookup."""
    key_lower = key.lower()
    for k, v in request.query_params.items():
        if k.lower() == key_lower:
            return v
    return default


# ==========================================
# REST API & Real-time Web Monitor Endpoints
# ==========================================

@router.get("/api/punches")
async def get_punches():
    """API to get punch history."""
    return {"count": len(PUNCH_LOGS), "data": PUNCH_LOGS}


@router.post("/api/clear")
async def clear_punches():
    """Clear in-memory and local file punch history."""
    global PUNCH_LOGS
    PUNCH_LOGS = []
    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    except Exception:
        pass
    return {"success": True, "message": "Logs cleared"}


@router.get("/api/users")
async def get_all_users():
    """Get all cached user names and roles."""
    return {"count": len(DEVICE_USER_CACHE), "users": DEVICE_USER_CACHE}


@router.post("/api/users/set_name")
async def set_user_name(request: Request):
    """Set or update an employee/user name by User ID / PIN."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})

    user_id = str(data.get("userId", "")).strip()
    name = str(data.get("name", "")).strip()
    role = str(data.get("role", "")).strip()

    if not user_id or not name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "userId and name are required"})

    current_info = DEVICE_USER_CACHE.get(user_id, {})
    DEVICE_USER_CACHE[user_id] = {
        "name": name,
        "role": role if role else current_info.get("role", "Normal User"),
        "privilege": current_info.get("privilege", 0),
        "card": current_info.get("card", 0)
    }
    save_user_cache()

    # Update in-memory punch records and file
    updated_count = 0
    for p in PUNCH_LOGS:
        if str(p.get("userId", "")).strip() == user_id:
            p["userName"] = name
            if role:
                p["userRole"] = role
            updated_count += 1

    try:
        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(PUNCH_LOGS, f, indent=2, default=str)
    except Exception:
        pass

    print(f"\033[1;32m[User Name Saved]\033[0m Mapped ID \033[1;33m{user_id}\033[0m -> \033[1;97m{name}\033[0m ({updated_count} punches updated)")
    return {"ok": True, "message": f"Updated name for User {user_id} to '{name}'", "updatedPunches": updated_count}


@router.get("/events")
async def sse_events(request: Request):
    """Real-time SSE (Server-Sent Events) endpoint for live dashboard updates."""
    queue = asyncio.Queue()
    SSE_SUBSCRIBERS.append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in SSE_SUBSCRIBERS:
                SSE_SUBSCRIBERS.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
        }
    )


@router.get("/api/device/status")
async def get_device_status(sn: str):
    """Device status check (in-memory tracking without database dependency)."""
    last_seen_time = LAST_SYNC_TIMES.get(sn)
    if not last_seen_time:
        return {"status": "offline", "last_seen": None, "sn": sn}

    diff = time.time() - last_seen_time
    is_online = diff < 120
    return {
        "status": "online" if is_online else "offline",
        "last_seen": datetime.fromtimestamp(last_seen_time).isoformat(),
        "sn": sn,
        "seconds_since_last_seen": diff
    }


# ==========================================
# eSSL / ZKTeco ADMS & iClock Protocol Endpoints
# Standalone Live Push Capture (Zero Database Dependency)
# ==========================================

@router.api_route("/iclock/cdata", methods=["GET", "POST"])
@router.api_route("/cdata", methods=["GET", "POST"])
@router.api_route("/iclock/cdata.aspx", methods=["GET", "POST"])
@router.api_route("/cdata.aspx", methods=["GET", "POST"])
@router.api_route("/iclock/cdata.php", methods=["GET", "POST"])
@router.api_route("/cdata.php", methods=["GET", "POST"])
@router.api_route("/iclock/rtlog", methods=["GET", "POST"])
@router.api_route("/rtlog", methods=["GET", "POST"])
@router.api_route("/iclock/rtlog.aspx", methods=["GET", "POST"])
@router.api_route("/rtlog.aspx", methods=["GET", "POST"])
@router.api_route("/iclock/rtlog.php", methods=["GET", "POST"])
@router.api_route("/rtlog.php", methods=["GET", "POST"])
@router.api_route("/iclock/rtstate", methods=["GET", "POST"])
@router.api_route("/rtstate", methods=["GET", "POST"])
async def cdata_handler(request: Request) -> PlainTextResponse:
    sn = get_query_param_ci(request, "SN") or "DEFAULT"
    table = (get_query_param_ci(request, "table") or "").upper()
    client_ip = request.client.host if request.client else "unknown"
    # A handshake is forced past the throttle: it is a reader announcing itself
    # after a restart or a config change, and that is the one contact worth
    # recording the instant it happens rather than up to a minute later.
    mark_seen(sn, request, force=request.method == "GET")

    # 1.1 Device Handshake / Initialization (GET)
    if request.method == "GET":
        print(f"\n\033[1;32m[iClock Handshake Connected]\033[0m Device SN: \033[1;33m{sn}\033[0m from IP: \033[1;36m{client_ip}\033[0m")

        config_response = "\n".join([
            f"GET OPTION FROM: {sn}",
            "Stamp=0",
            "OpStamp=0",
            "PhotoStamp=0",
            "ErrorDelay=10",
            "Delay=5",
            "TransTimes=00:00;14:05",
            "TransInterval=1",
            "TransFlag=1111000000",
            "Realtime=1",
            "RealTime=1",
            "Encrypt=0",
            "ServerVersion=3.0.1",
            "PushVersion=3.2.1",
            "PushOptionsFlag=1",
            "PushProtVer=3.2.1"
        ])
        return PlainTextResponse(content=config_response, media_type="text/plain")

    # 1.2 Data / Attendance / Real-time Push (POST)
    raw_body = await request.body()
    body_str = raw_body.decode(errors="ignore")

    # Detect table from query param or body header
    effective_table = table
    if not effective_table:
        first_line = body_str.split("\n")[0].strip().upper() if body_str else ""
        if "ATTLOG" in first_line:
            effective_table = "ATTLOG"
        elif "OPERLOG" in first_line or "OPLOG" in first_line:
            effective_table = "OPERLOG"
        elif "RTLOG" in first_line:
            effective_table = "RTLOG"
        else:
            effective_table = "ATTLOG"

    # Route by Table Type
    if "OPERLOG" in effective_table or "OPLOG" in effective_table:
        parse_oplog(body_str, sn)
        return PlainTextResponse(content="OK", media_type="text/plain")

    # Process Attendance / Realtime punches (ATTLOG, RTLOG, RECORD, or default)
    new_punches = parse_attlog(body_str, sn)
    if new_punches:
        for punch in new_punches:
            print(format_punch_banner(punch, client_ip))
            broadcast_punch(punch)

        # Into the LMS as well as onto the dashboard. Imported here rather than
        # at module import so this router still loads on a host where the sync
        # package's dependencies are missing — the local dashboard is expected
        # to work standalone.
        #
        # A pushed punch arrives exactly once: the reader will not offer it
        # again, and unlike a polled device there is nothing to re-read. So
        # `ingest` spools on failure and never raises, and this handler always
        # answers OK.
        try:
            from app.sync.push_ingest import ingest

            usable, stored = ingest(new_punches)
            if usable and not stored:
                print(f"\033[1;33m[LMS]\033[0m {sn}: {usable} pushed punch(es) were "
                      f"already stored or are held on disk")
            elif stored:
                print(f"\033[1;34m[LMS]\033[0m {sn}: stored {stored} new punch(es)")
        except Exception as exc:
            # Recorded against the reader so the console shows why its punches
            # stopped landing. It does NOT mark the device down — it called in,
            # which is why we are here; see `app.sync.liveness`.
            print(f"\033[1;31m[LMS]\033[0m {sn}: sync unavailable ({exc})")
            with suppress(Exception):
                from app.sync.liveness import record_traffic_error

                record_traffic_error(sn, f"{type(exc).__name__}: {exc}")
    else:
        print(f"\033[36m[iClock Data POST]\033[0m Device: \033[33m{sn}\033[0m | Table: {effective_table or 'N/A'} | Bytes: {len(raw_body)}")
        if body_str and len(body_str) < 300:
            print(f"   Payload: {body_str.strip()}")

    return PlainTextResponse(content="OK", media_type="text/plain")


# 2. GETREQUEST (Command Polling)
@router.api_route("/iclock/getrequest", methods=["GET", "POST"])
@router.api_route("/getrequest", methods=["GET", "POST"])
@router.api_route("/iclock/getrequest.aspx", methods=["GET", "POST"])
@router.api_route("/getrequest.aspx", methods=["GET", "POST"])
@router.api_route("/iclock/getrequest.php", methods=["GET", "POST"])
@router.api_route("/getrequest.php", methods=["GET", "POST"])
async def getrequest_handler(request: Request) -> PlainTextResponse:
    # The steady-state heartbeat. A reader polls this every few seconds for
    # commands even when nobody has touched it all morning, which is exactly
    # what makes it the right signal for "is this thing alive" — a quiet reader
    # and a dead one are indistinguishable by punches alone.
    sn = get_query_param_ci(request, "SN")
    mark_seen(sn, request)
    return PlainTextResponse("OK", media_type="text/plain")


# 3. DEVICECMD (Command Acknowledgment)
@router.api_route("/iclock/devicecmd", methods=["GET", "POST"])
@router.api_route("/devicecmd", methods=["GET", "POST"])
@router.api_route("/iclock/devicecmd.aspx", methods=["GET", "POST"])
@router.api_route("/devicecmd.aspx", methods=["GET", "POST"])
@router.api_route("/iclock/devicecmd.php", methods=["GET", "POST"])
@router.api_route("/devicecmd.php", methods=["GET", "POST"])
async def devicecmd_handler(request: Request) -> PlainTextResponse:
    sn = get_query_param_ci(request, "SN")
    raw_body = await request.body()
    body_str = raw_body.decode(errors="ignore")
    mark_seen(sn, request)
    print(f"\033[34m[iClock DeviceCmd Response]\033[0m Device: {sn} | Body: {body_str}")
    return PlainTextResponse("OK", media_type="text/plain")


# 4. FDATA (Biometric template, photo, file push)
@router.api_route("/iclock/fdata", methods=["GET", "POST"])
@router.api_route("/fdata", methods=["GET", "POST"])
@router.api_route("/iclock/fdata.aspx", methods=["GET", "POST"])
@router.api_route("/fdata.aspx", methods=["GET", "POST"])
@router.api_route("/iclock/fdata.php", methods=["GET", "POST"])
@router.api_route("/fdata.php", methods=["GET", "POST"])
async def fdata_handler(request: Request) -> PlainTextResponse:
    table = get_query_param_ci(request, "table", "N/A")
    mark_seen(get_query_param_ci(request, "SN"), request)
    print(f"\033[34m[iClock FData Push]\033[0m Table: {table}")
    return PlainTextResponse("OK", media_type="text/plain")


# 5. PUSH / REGISTRY / PING heartbeats
#
# NOTE ON THE NAME: `/iclock/ping` is the DEVICE pinging US. It is inbound —
# some firmware calls it before it will start pushing — and it is the opposite
# of the outbound probe this service used to run. Keep it; it is one more piece
# of evidence that a reader is alive, arriving unasked.
@router.api_route("/iclock/push", methods=["GET", "POST"])
@router.api_route("/push", methods=["GET", "POST"])
@router.api_route("/iclock/ping", methods=["GET", "POST"])
@router.api_route("/ping", methods=["GET", "POST"])
@router.api_route("/iclock/registry", methods=["GET", "POST"])
@router.api_route("/registry", methods=["GET", "POST"])
async def ping_handler(request: Request) -> PlainTextResponse:
    """Heartbeat routes. Some firmware calls these before it will push."""
    mark_seen(get_query_param_ci(request, "SN"), request)
    return PlainTextResponse("OK", media_type="text/plain")

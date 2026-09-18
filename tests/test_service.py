"""
What this service has to get right, in one file.

Scraped deliberately. The two test modules that used to live here were written
against a different legacy schema, `DATA UPDATE USERINFO`
replies, a JSON root route — and asserted behaviour this service has never
had. They could not pass, which is presumably why the pytest step in CI was
commented out. A suite that cannot pass is worse than none: it reports green
by not running.

So this covers only what is both true and worth breaking the build over:

  * the punch pipeline's pure logic — the spool round-trip and the wire
    parser, where a silent bug costs somebody their attendance
  * the ADMS endpoint still answers OK, including when the LMS is unreachable,
    because a reader that gets an error back may drop the batch forever

Nothing here touches a database or a device. The LMS connection belongs to the
LMS, and pyzk needs hardware.
"""

from datetime import datetime

from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.router import iclock
from app.sync.models import Punch
from app.sync.push_ingest import to_punch
from app.sync.spool import PunchSpool

client = TestClient(app)


# ----------------------------------------------------------------------
# The punch itself
# ----------------------------------------------------------------------

def test_punch_survives_the_spool_unchanged() -> None:
    """
    A punch that went to disk must come back identical.

    The spool is the last copy of a pushed punch before it reaches the LMS, so
    a lossy round-trip here is attendance quietly disappearing.
    """
    punch = Punch("IP:192.168.1.209", "102", datetime(2026, 8, 15, 22, 12, 5),
                  punch_state=0, verify_mode=1, work_code="0",
                  device_user_name="Gowtham")
    assert Punch.from_json(punch.to_json()) == punch


def test_punch_time_stays_naive() -> None:
    """
    The reader reports wall clock with no zone, and it must stay that way.

    Attaching a timezone here would mean this bridge guessing on behalf of the
    LMS, which is the component that actually knows the answer.
    """
    punch = Punch("ZK1", "102", datetime(2026, 8, 15, 22, 12, 5))
    assert Punch.from_json(punch.to_json()).device_time.tzinfo is None


def test_spool_drain_empties_and_restore_puts_back(tmp_path) -> None:
    spool = PunchSpool(str(tmp_path / "spool.jsonl"))
    punches = [
        Punch("ZK1", "102", datetime(2026, 8, 15, 9, 0, 0)),
        Punch("ZK1", "103", datetime(2026, 8, 15, 9, 1, 0)),
    ]
    spool.add(punches)
    assert spool.pending() == 2

    drained = spool.drain()
    assert len(drained) == 2
    assert spool.pending() == 0, "drain must take rows out, or they send twice"

    spool.restore(drained)
    assert spool.pending() == 2, "a failed batch must be recoverable"


def test_spool_skips_a_corrupt_line_without_stranding_the_rest(tmp_path) -> None:
    """
    One unparseable line must not block the spool.

    It can never parse, so retrying it forever would hold up every good punch
    behind it.
    """
    path = tmp_path / "spool.jsonl"
    good = Punch("ZK1", "102", datetime(2026, 8, 15, 9, 0, 0))
    path.write_text(good.to_json() + "\n{not json\n", encoding="utf-8")

    recovered = PunchSpool(str(path)).drain()
    assert len(recovered) == 1
    assert recovered[0] == good


# ----------------------------------------------------------------------
# The wire parser
# ----------------------------------------------------------------------

def test_pushed_row_becomes_a_punch() -> None:
    punch = to_punch({
        "userId": "102", "timestamp": "2026-08-15 22:12:05", "sn": "ZK1",
        "status": "0", "verifyType": "1", "workCode": "0",
    })
    assert punch is not None
    assert punch.key == ("ZK1", "102", datetime(2026, 8, 15, 22, 12, 5))
    assert punch.punch_state == 0
    assert punch.verify_mode == 1


def test_placeholder_name_is_dropped() -> None:
    """
    The router substitutes "User 102" when it has no real name.

    Carrying that through would put it in the console beside an unmapped PIN,
    where it reads as the name somebody typed into the reader.
    """
    punch = to_punch({"userId": "102", "timestamp": "2026-08-15 22:12:05",
                      "sn": "ZK1", "userName": "User 102"})
    assert punch is not None
    assert punch.device_user_name is None


def test_unusable_rows_are_skipped_not_stored() -> None:
    assert to_punch({"userId": "", "timestamp": "2026-08-15 22:12:05"}) is None
    assert to_punch({"userId": "102", "timestamp": "not a time"}) is None
    assert to_punch({"userId": "102"}) is None


def test_userinfo_keeps_excel_name_with_spaces(monkeypatch) -> None:
    """eSSL USERINFO exports use tabs and names commonly contain spaces."""
    monkeypatch.setattr(iclock, "save_device_user_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(iclock, "save_user_cache", lambda: None)
    iclock.DEVICE_USER_CACHE.pop("550", None)

    iclock.parse_userinfo("PIN=550\tName=Deepak Kumar\tPri=0\tCard=0", "ZK1")

    assert iclock.DEVICE_USER_CACHE["550"]["name"] == "Deepak Kumar"


def test_operlog_user_profile_is_saved_as_adms_user(monkeypatch) -> None:
    """NFZ firmware can return USERINFO rows under the OPERLOG table."""
    monkeypatch.setattr(iclock, "save_device_user_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(iclock, "save_user_cache", lambda: None)
    iclock.DEVICE_USER_CACHE.pop("007", None)

    discovered = iclock.parse_oplog("USER PIN=007 Name=Gowtham Test Pri=0", "ZK1")

    assert discovered == 1
    assert iclock.DEVICE_USER_CACHE["007"]["name"] == "Gowtham Test"
    assert iclock.DEVICE_USER_CACHE["007"]["deviceSerial"] == "ZK1"


def test_operlog_tab_separated_user_record_with_type_prefix(monkeypatch) -> None:
    """The shape NFZ firmware actually sends: record-type prefix AND tabs.

    "USER PIN=3" is one tab field, so its key parsed as "USER PIN" rather than
    "PIN" and every name the reader sent was dropped. The tests above each
    cover only half of this shape, which is how it went unnoticed.
    """
    monkeypatch.setattr(iclock, "save_device_user_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(iclock, "save_user_cache", lambda: None)

    # The FP line triggers a name lookup that reads Postgres; keep this test
    # independent of whatever a developer's local database happens to hold.
    from app.sync import lms_db

    def _no_database(self):
        raise lms_db.LmsUnavailableError("no database in this test")

    monkeypatch.setattr(lms_db.LmsDatabase, "_connection", _no_database)
    for pin in ("3", "009"):
        iclock.DEVICE_USER_CACHE.pop(pin, None)

    body = "\n".join([
        "USER PIN=3\tName=Aki\tPri=0\tPasswd=\tCard=12345\tGrp=1\tTZ=0000000000000000",
        "FP PIN=3\tFID=6\tSize=1448\tValid=1\tTMP=AAAA",
        "USER PIN=009\tName=Deepak Kumar\tPri=14\tPasswd=\tCard=\tGrp=1",
    ])
    discovered = iclock.parse_oplog(body, "NFZ8254900401")

    assert discovered == 2
    assert iclock.DEVICE_USER_CACHE["3"]["name"] == "Aki"
    assert iclock.DEVICE_USER_CACHE["3"]["card"] == "12345"
    assert iclock.DEVICE_USER_CACHE["009"]["name"] == "Deepak Kumar"
    assert iclock.DEVICE_USER_CACHE["009"]["role"] == "Super Admin"
    assert iclock.DEVICE_USER_CACHE["009"]["deviceSerial"] == "NFZ8254900401"


def test_db_name_lookup_keeps_card_and_never_borrows_another_branch(monkeypatch) -> None:
    """A DB-resolved name must not wipe the card USERINFO just cached for this
    device — and must never inherit another branch's card for the same PIN."""
    from contextlib import contextmanager

    from app.sync import lms_db

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            return ("Aki", "Normal User")

    class _Conn:
        def cursor(self):
            return _Cursor()

    @contextmanager
    def _fake_connection(self):
        yield _Conn()

    monkeypatch.setattr(lms_db.LmsDatabase, "_connection", _fake_connection)
    monkeypatch.setattr(iclock, "save_user_cache", lambda: None)

    monkeypatch.setitem(iclock.DEVICE_USER_CACHE, "3", {
        "name": "Aki", "role": "Normal User", "deviceSerial": "NFZ8254900401",
        "privilege": 0, "card": "12345",
    })
    info = iclock.get_user_info("3", "NFZ8254900401")
    assert info["name"] == "Aki"
    assert info["card"] == "12345"

    monkeypatch.setitem(iclock.DEVICE_USER_CACHE, "3", {
        "name": "Someone Else", "deviceSerial": "OTHERBRANCH01", "card": "99999",
    })
    info = iclock.get_user_info("3", "NFZ8254900401")
    assert info["name"] == "Aki"
    assert "card" not in info


# ----------------------------------------------------------------------
# User refreshes must not loop: 100 users x 20 readers re-pulled every few
# seconds is megabytes of fingerprint templates and thousands of DB writes.
# ----------------------------------------------------------------------

def _isolate_adms_state(monkeypatch, db_saves: list | None = None) -> None:
    """Fresh command/refresh state, and no files, database or clock surprises."""
    from app.sync import lms_db

    def _no_database(self):
        raise lms_db.LmsUnavailableError("no database in this test")

    monkeypatch.setattr(lms_db.LmsDatabase, "_connection", _no_database)
    monkeypatch.setattr(iclock, "COMMAND_TRACKING", {})
    monkeypatch.setattr(iclock, "DEVICE_COMMAND_QUEUE", {})
    monkeypatch.setattr(iclock, "_LAST_USERINFO_REFRESH", {})
    monkeypatch.setattr(iclock, "_USERINFO_ASKED_FOR", {})
    monkeypatch.setattr(iclock, "_PERSISTED_DEVICE_USERS", {})
    monkeypatch.setattr(iclock, "DEVICE_USER_CACHE", {})
    monkeypatch.setattr(iclock, "save_adms_state", lambda: None)
    monkeypatch.setattr(iclock, "save_user_cache", lambda: None)
    monkeypatch.setattr(iclock, "save_device_registry", lambda: None)

    def _save(*args, **kwargs):
        if db_saves is not None:
            db_saves.append(args[:3])
        return True

    monkeypatch.setattr(iclock, "save_device_user_db", _save)


def _userinfo_queries(sn: str) -> list[str]:
    return [cmd for cmd in iclock.DEVICE_COMMAND_QUEUE.get(sn, []) if "DATA QUERY USERINFO" in cmd]


def test_userinfo_answer_in_two_posts_does_not_requery(monkeypatch) -> None:
    """The exact production loop: users arrive in one post, fingerprints in the
    next. The fingerprint post names no users, which used to trigger another
    DATA QUERY USERINFO — forever."""
    _isolate_adms_state(monkeypatch)
    sn = "LOOPTEST01"

    users_post = "USER PIN=1\tName=Admin\tPri=14\tCard=\tGrp=1\nUSER PIN=2\tName=Deepak\tPri=14\tCard=\tGrp=1"
    fingerprints_post = "FP PIN=1\tFID=6\tSize=1116\tValid=1\tTMP=AAAA\nFP PIN=2\tFID=6\tSize=1100\tValid=1\tTMP=BBBB"

    for body in (users_post, fingerprints_post):
        response = client.post(f"/iclock/cdata?SN={sn}&table=OPERLOG&Stamp=9999", content=body)
        assert response.text == "OK"

    assert _userinfo_queries(sn) == []


def test_unchanged_users_are_not_rewritten_to_the_database(monkeypatch) -> None:
    db_saves: list = []
    _isolate_adms_state(monkeypatch, db_saves)
    body = "USER PIN=1\tName=Admin\tPri=14\nUSER PIN=2\tName=Deepak\tPri=0"

    assert iclock.parse_userinfo(body, "NFZ8254900401") == 2
    assert len(db_saves) == 2

    # The same answer again — the reader re-sends its whole table each time.
    assert iclock.parse_userinfo(body, "NFZ8254900401") == 2
    assert len(db_saves) == 2

    # A renamed user is written, and only that one.
    iclock.parse_userinfo("USER PIN=2\tName=Deepak Kumar\tPri=0", "NFZ8254900401")
    assert db_saves[-1] == ("NFZ8254900401", "2", "Deepak Kumar")
    assert len(db_saves) == 3


def test_unknown_pin_refresh_is_rate_limited(monkeypatch) -> None:
    _isolate_adms_state(monkeypatch)
    sn = "RATELIMIT01"
    clock = [1_000_000.0]
    monkeypatch.setattr(iclock.time, "time", lambda: clock[0])

    assert iclock.request_userinfo_refresh(sn, ["9"]) is True
    # Still queued: no second copy, even for a different unknown PIN.
    assert iclock.request_userinfo_refresh(sn, ["10"]) is False
    assert len(_userinfo_queries(sn)) == 1

    # Answered, but within the device cooldown.
    for cmd in iclock.COMMAND_TRACKING.values():
        cmd["status"] = "acknowledged"
    clock[0] += 60
    assert iclock.request_userinfo_refresh(sn, ["10"]) is False

    # Past the cooldown: a PIN already asked about (the reader has no name
    # for it) is not worth another full pull, but a new unknown PIN is.
    clock[0] += iclock.USERINFO_DEVICE_COOLDOWN_SECONDS
    assert iclock.request_userinfo_refresh(sn, ["9"]) is False
    assert iclock.request_userinfo_refresh(sn, ["10"]) is True


# ----------------------------------------------------------------------
# The ADMS endpoints
# ----------------------------------------------------------------------

def test_handshake_returns_device_options() -> None:
    response = client.get("/iclock/cdata?SN=ZK1")
    assert response.status_code == status.HTTP_200_OK
    assert "GET OPTION FROM" in response.text


def test_adms_onboarding_queues_user_data_before_30_day_attendance(monkeypatch) -> None:
    """Onboarding uses only the device-initiated ADMS command channel."""
    monkeypatch.setattr(iclock, "save_adms_state", lambda: None)
    iclock.DEVICE_COMMAND_QUEUE.clear()
    iclock.COMMAND_TRACKING.clear()
    iclock.ADMS_IMPORT_JOBS.clear()

    job = iclock.start_adms_onboarding("TEST-ADMS")
    commands = iclock.DEVICE_COMMAND_QUEUE["TEST-ADMS"]

    assert len(commands) == 2
    assert commands[0].endswith("DATA QUERY USERINFO")
    assert "DATA QUERY ATTLOG StartTime=" in commands[1]
    assert "\tEndTime=" in commands[1]
    assert job["days"] == 30


def test_adms_import_tracks_attlog_acknowledgement(monkeypatch) -> None:
    monkeypatch.setattr(iclock, "save_adms_state", lambda: None)
    iclock.DEVICE_COMMAND_QUEUE.clear()
    iclock.COMMAND_TRACKING.clear()
    iclock.ADMS_IMPORT_JOBS.clear()

    job = iclock.start_adms_onboarding("TEST-ACK")
    attlog_id = job["commands"][1].split(":", 2)[1]
    iclock.record_import_users("TEST-ACK", 3)
    iclock.record_import_punches("TEST-ACK", 8, 5)
    iclock.record_command_result("TEST-ACK", f"ID={attlog_id}&Return=0&CMD=DATA")

    assert job["usersReceived"] == 3
    assert job["punchesReceived"] == 8
    assert job["punchesStored"] == 5
    assert job["duplicates"] == 3
    assert job["status"] == "completed"


def test_push_is_acknowledged_even_with_no_lms() -> None:
    """
    The single most important behaviour of the push path.

    No LMS is configured in the test environment, so `ingest` cannot store
    anything. The reader must still get OK: firmware that receives an error may
    drop the batch, and a pushed punch is never offered again.
    """
    response = client.post(
        "/iclock/cdata?SN=ZK1&table=ATTLOG",
        content="102\t2026-08-15 22:12:05\t0\t1\t0",
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.text == "OK"


def test_pulse_is_alive() -> None:
    response = client.get("/api/pulse")
    assert response.status_code == status.HTTP_200_OK


def test_health_tells_the_truth_about_the_lms() -> None:
    """
    Health must report the real state, never a cheerful 200 regardless.

    The old check `SELECT 1`-ed a local database nothing used, so it went green
    while punches piled up unsent — the exact failure this replaces.

    Asserted as a contract rather than a fixed answer. An earlier version of
    this test hard-coded 503/"unconfigured", which passed only on a machine
    with no .env and started failing the moment the bridge was actually wired
    to the LMS — a test that breaks when the system starts working is worse
    than no test.
    """
    response = client.get("/api/health")
    body = response.json()
    state = body["status"]

    if state == "disabled":
        # Switched off on purpose — which is the state conftest puts the suite
        # in. Not an error: nothing is expected to be syncing.
        assert response.status_code == status.HTTP_200_OK
    elif state == "ok":
        # Configured and reachable.
        assert response.status_code == status.HTTP_200_OK
        assert body["lms_reachable"] is True
        assert "punches_held_on_disk" in body
    else:
        # Meant to be syncing and cannot. Must refuse, with an actionable reason.
        assert state in {"unconfigured", "degraded"}, f"unexpected status {state!r}"
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

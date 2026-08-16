"""
What this service has to get right, in one file.

Scoped deliberately. The two test modules that used to live here were written
against a different application — a tenant/gym schema, `DATA UPDATE USERINFO`
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
    punch = Punch("IP:192.168.0.210", "102", datetime(2026, 8, 15, 22, 12, 5),
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


# ----------------------------------------------------------------------
# The ADMS endpoints
# ----------------------------------------------------------------------

def test_handshake_returns_device_options() -> None:
    response = client.get("/iclock/cdata?SN=ZK1")
    assert response.status_code == status.HTTP_200_OK
    assert "GET OPTION FROM" in response.text


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

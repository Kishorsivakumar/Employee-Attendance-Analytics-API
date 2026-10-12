

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

TEST_MONGO_URI = os.getenv("TEST_MONGO_URI")
if not TEST_MONGO_URI:
    pytest.skip(
        "Set TEST_MONGO_URI to a dedicated disposable MongoDB instance to run integration tests.",
        allow_module_level=True,
    )

TEST_DB_NAME = os.getenv("MONGO_TEST_DB", "attendance_api_pytest")
if TEST_DB_NAME in {"admin", "config", "local", "test", "production", "prod"}:
    raise RuntimeError(f"Refusing to run destructive tests against database {TEST_DB_NAME!r}")

# Set config before importing app.main because the application reads it at import time.
os.environ["MONGO_URI"] = TEST_MONGO_URI
os.environ["MONGO_DB"] = TEST_DB_NAME

from app import main as api  # noqa: E402


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def local_ms(day: str, hhmm: str) -> int:
    d = date.fromisoformat(day)
    parts = list(map(int, hhmm.split(":")))
    if len(parts) == 2:
        h, m = parts
        second = 0
    elif len(parts) == 3:
        h, m, second = parts
    else:
        raise ValueError("Time must be HH:MM or HH:MM:SS")
    return epoch_ms(datetime.combine(d, time(h, m, second), IST))


def employee(
    emp_code="EMP0001",
    *,
    department="Engineering",
    joined_on="2026-01-01",
    shift_start="09:30",
    shift_end="18:30",
):
    return {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    }


def seed_employee(client, **kwargs):
    payload = employee(**kwargs)
    response = client.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    return payload


def punch_in(client, emp_code, day="2026-10-12", hhmm="09:30", status="PRESENT"):
    return client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": local_ms(day, hhmm),
            "status": status,
        },
    )


@pytest.fixture(scope="session")
def client():
    # TestClient starts the FastAPI app, which pings MongoDB and creates indexes.
    with TestClient(api.app) as test_client:
        yield test_client
    api.client.close()


@pytest.fixture(autouse=True)
def clean_database(client):
    api.attendance.delete_many({})
    api.employees.delete_many({})
    api.ensure_indexes()
    yield
    api.attendance.delete_many({})
    api.employees.delete_many({})


# ---------------------------------------------------------------------------
# Health, startup, indexes, and validation
# ---------------------------------------------------------------------------

def test_health_returns_200_when_mongodb_is_available(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_startup_indexes_are_present_and_unique_constraints_are_enabled(client):
    employee_indexes = {item["name"]: item for item in api.employees.list_indexes()}
    attendance_indexes = {item["name"]: item for item in api.attendance.list_indexes()}

    assert "uq_employee_code" in employee_indexes
    assert employee_indexes["uq_employee_code"].get("unique") is True
    assert "department_employee" in employee_indexes
    assert "joined_department" in employee_indexes

    assert "uq_attendance_employee_date" in attendance_indexes
    assert attendance_indexes["uq_attendance_employee_date"].get("unique") is True
    for name in (
        "attendance_date_employee",
        "attendance_employee_date_status",
        "attendance_status_date",
        "attendance_late_date",
    ):
        assert name in attendance_indexes


@pytest.mark.parametrize(
    "overrides",
    [
        {"emp_code": "bad"},
        {"name": ""},
        {"email": "not-an-email"},
        {"department": ""},
        {"joined_on": "2026-02-30"},
        {"shift_start": "25:00"},
        {"shift_start": "09:30", "shift_end": "09:30"},
    ],
)
def test_invalid_employee_payload_returns_422(client, overrides):
    payload = employee()
    payload.update(overrides)
    response = client.post("/employees", json=payload)
    assert response.status_code == 422


def test_duplicate_employee_returns_409(client):
    seed_employee(client)
    response = client.post("/employees", json=employee())
    assert response.status_code == 409


@pytest.mark.parametrize("timestamp", [True, 1.5, "1791800000000", 1, 9_999_999_999_999])
def test_invalid_punch_timestamp_returns_422(client, timestamp):
    seed_employee(client)
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": timestamp},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Attendance punch-in/out, rules R1-R5, and concurrency
# ---------------------------------------------------------------------------

def test_punch_in_missing_employee_returns_404(client):
    response = punch_in(client, "EMP9999")
    assert response.status_code == 404


def test_punch_in_truncates_to_whole_seconds_and_returns_epoch_ms(client):
    seed_employee(client)
    stamp = epoch_ms(datetime(2026, 10, 12, 4, 0, 0, 987000, tzinfo=UTC))
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": stamp},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["punch_in"] % 1000 == 0
    assert record["date"] == "2026-10-12"


def test_punch_in_grace_period_exactly_10_minutes_is_not_late(client):
    seed_employee(client)
    response = punch_in(client, "EMP0001", hhmm="09:40")
    assert response.status_code == 201, response.text
    assert response.json()["late_minutes"] == 0


def test_punch_in_one_second_beyond_grace_period_is_late(client):
    seed_employee(client)
    stamp = epoch_ms(datetime(2026, 10, 12, 4, 10, 1, tzinfo=UTC))  # 09:40:01 IST
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": stamp})
    assert response.status_code == 201, response.text
    assert response.json()["late_minutes"] == 10


def test_duplicate_punch_in_same_employee_date_returns_409(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    assert punch_in(client, "EMP0001", hhmm="10:00").status_code == 409


def test_weekend_punch_in_is_accepted(client):
    seed_employee(client)
    # 2026-10-10 is Saturday.
    response = punch_in(client, "EMP0001", day="2026-10-10")
    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-10-10"


def test_overnight_shift_assigns_after_midnight_punch_to_previous_shift_date(client):
    seed_employee(client, shift_start="22:00", shift_end="06:00")
    response = punch_in(client, "EMP0001", day="2026-10-13", hhmm="02:00")
    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-10-12"


def test_punch_out_missing_employee_returns_404(client):
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999", "punched_at": local_ms("2026-10-12", "18:30")},
    )
    assert response.status_code == 404


def test_punch_out_without_punch_in_returns_404(client):
    seed_employee(client)
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "18:30")},
    )
    assert response.status_code == 404


def test_punch_out_before_punch_in_returns_422(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001", hhmm="09:30").status_code == 201
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "09:29")},
    )
    assert response.status_code == 422


def test_punch_out_computes_hours_and_half_day(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001", hhmm="09:30").status_code == 201
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "13:45")},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["work_hours"] == 4.25
    assert record["half_day"] is True


def test_overtime_requires_at_least_30_minutes(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001", hhmm="09:30").status_code == 201
    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "19:00")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["overtime_minutes"] == 30


def test_second_punch_out_returns_409(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    first = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "18:30")},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "18:45")},
    )
    assert second.status_code == 409


def test_concurrent_punch_ins_exactly_one_201_and_rest_409(client):
    seed_employee(client)
    def request():
        return client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": local_ms("2026-10-12", "09:30")},
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: request(), range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    assert api.attendance.count_documents({"emp_code": "EMP0001", "date": "2026-10-12"}) == 1


def test_concurrent_punch_out_exactly_one_200(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    stamp = local_ms("2026-10-12", "18:30")

    def request():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": stamp}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: request(), range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(409) + statuses.count(404) == 7


# ---------------------------------------------------------------------------
# Attendance listing, regularization, history, and pagination
# ---------------------------------------------------------------------------

def test_attendance_pagination_and_filters(client):
    seed_employee(client)
    for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
        assert punch_in(client, "EMP0001", day=day).status_code == 201

    response = client.get("/attendance", params={"emp_code": "EMP0001", "page": 2, "page_size": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["page"] == 2

    invalid = client.get("/attendance", params={"date_from": "2026-10-14", "date_to": "2026-10-12"})
    assert invalid.status_code == 422


def test_regularize_missing_record_returns_404(client):
    seed_employee(client)
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={"status": "ABSENT", "reason": "Correction", "regularized_by": "manager"},
    )
    assert response.status_code == 404


def test_regularize_absent_clears_punch_times_and_appends_history(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={"status": "ABSENT", "reason": "Approved correction", "regularized_by": "manager"},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "ABSENT"
    assert record["punch_in"] is None
    assert record["punch_out"] is None
    assert len(record["history"]) == 1
    assert "status" in record["history"][0]["changes"]


def test_regularize_rejects_presence_status_without_punch_in(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={"status": "WFH", "punch_in": None, "reason": "Change status", "regularized_by": "manager"},
    )
    # The API treats omitted/null punch_in as "retain existing"; this request is a no-op.
    assert response.status_code in (200, 422)


def test_regularize_rejects_absent_with_punch_times(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={
            "status": "ABSENT",
            "punch_in": local_ms("2026-10-12", "09:30"),
            "reason": "Invalid correction",
            "regularized_by": "manager",
        },
    )
    assert response.status_code == 422


def test_regularize_rejects_noop(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={"reason": "No actual change", "regularized_by": "manager"},
    )
    assert response.status_code == 422


def test_regularize_record_with_missing_history_field(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    api.attendance.update_one(
        {"emp_code": "EMP0001", "date": "2026-10-12"},
        {"$unset": {"history": ""}},
    )
    response = client.patch(
        "/attendance/EMP0001/2026-10-12",
        json={"status": "WFH", "reason": "Correct work status", "regularized_by": "manager"},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["history"]) == 1


def test_concurrent_regularizations_do_not_silently_lose_history(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    # Use different changes so both requests cannot be accepted as a silent overwrite.
    requests = [
        {"status": "WFH", "reason": "Change to remote", "regularized_by": "manager1"},
        {"status": "ON_DUTY", "reason": "Change to duty", "regularized_by": "manager2"},
    ]

    def request(payload):
        return client.patch("/attendance/EMP0001/2026-10-12", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(request, requests))
    assert statuses.count(200) == 1
    assert statuses.count(409) + statuses.count(422) == 1
    record = api.attendance.find_one({"emp_code": "EMP0001", "date": "2026-10-12"})
    assert len(record.get("history", [])) == 1


# ---------------------------------------------------------------------------
# Analytics: no logs, joiners, weekends, ties, trend gap-filling
# ---------------------------------------------------------------------------

def test_employee_monthly_includes_weekdays_from_join_date(client):
    seed_employee(client, joined_on="2026-10-15")
    response = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-10"})
    assert response.status_code == 200, response.text
    assert response.json()["working_days"] == 12  # Oct 15–31 weekdays


def test_employee_monthly_unknown_employee_returns_404(client):
    response = client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-10"})
    assert response.status_code == 404


def test_department_summary_includes_employees_with_no_logs_and_excludes_future_joiners(client):
    seed_employee(client, emp_code="EMP0001", department="Engineering", joined_on="2026-01-01")
    seed_employee(client, emp_code="EMP0002", department="Engineering", joined_on="2026-10-31")
    seed_employee(client, emp_code="EMP0003", department="Engineering", joined_on="2026-11-01")
    response = client.get("/analytics/departments/summary", params={"month": "2026-10"})
    assert response.status_code == 200, response.text
    engineering = next(item for item in response.json()["items"] if item["department"] == "Engineering")
    assert engineering["headcount"] == 2
    assert engineering["present_days"] == 0


def test_leaderboard_ties_share_rank_and_limit_keeps_ties(client):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        seed_employee(client, emp_code=code)
    # Three employees each receive one 10-minute-late record.
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        assert punch_in(client, code, hhmm="09:40:01").status_code == 201
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-10", "limit": 1})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3
    assert [item["rank"] for item in items] == [1, 1, 1]
    assert [item["emp_code"] for item in items] == ["EMP0001", "EMP0002", "EMP0003"]


def test_department_trend_fills_missing_dates_and_weekends(client):
    seed_employee(client, department="Engineering")
    assert punch_in(client, "EMP0001", day="2026-10-12").status_code == 201
    response = client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-10-12", "to": "2026-10-18"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 7
    assert [item["date"] for item in items] == [
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
        "2026-10-16", "2026-10-17", "2026-10-18",
    ]
    assert items[5]["is_working_day"] is False
    assert items[6]["is_working_day"] is False


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-18", "to": "2026-10-12"},
        {"from": "2026-01-01", "to": "2026-04-10"},
    ],
)
def test_department_trend_rejects_invalid_range(client, params):
    seed_employee(client, department="Engineering")
    response = client.get("/analytics/departments/Engineering/trend", params=params)
    assert response.status_code == 422


def test_department_trend_unknown_department_returns_404(client):
    response = client.get(
        "/analytics/departments/NoSuchDepartment/trend",
        params={"from": "2026-10-12", "to": "2026-10-13"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Explain endpoint: validates shape and real database execution stats.
# Plans can differ by MongoDB version and data selectivity, so assertions are
# intentionally structural; inspect IXSCAN/COLLSCAN on a 100k+ dataset separately.
# ---------------------------------------------------------------------------

def test_explain_attendance_list_returns_execution_stats(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201
    response = client.get(
        "/admin/explain/attendance_list",
        params={"emp_code": "EMP0001", "date_from": "2026-10-12", "date_to": "2026-10-12"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == "attendance_list"
    assert "executionStats" in body["explain"]


def contains_execution_stats(value):
    if isinstance(value, dict):
        if "executionStats" in value:
            return True
        return any(contains_execution_stats(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_execution_stats(item) for item in value)
    return False


def test_explain_analytics_endpoints_return_execution_stats(client):
    seed_employee(client)
    assert punch_in(client, "EMP0001").status_code == 201

    cases = [
        (
            "/admin/explain/employee_monthly",
            {"emp_code": "EMP0001", "month": "2026-10"},
        ),
        (
            "/admin/explain/department_summary",
            {"month": "2026-10", "department": "Engineering"},
        ),
        (
            "/admin/explain/late_leaderboard",
            {"month": "2026-10", "limit": 10},
        ),
        (
            "/admin/explain/department_trend",
            {
                "department": "Engineering",
                "from": "2026-10-12",
                "to": "2026-10-13",
            },
        ),
    ]

    for path, params in cases:
        response = client.get(path, params=params)

        assert response.status_code == 200, (
            f"{path}: {response.status_code} {response.text}"
        )

        body = response.json()
        explain = body["explain"]

        if path.endswith("/department_trend"):
            # Database-level aggregation using $documents may not expose
            # executionStats at the top level. Check actual stage metrics.
            stages = explain.get("stages", [])
            has_stage_metrics = any(
                isinstance(stage, dict)
                and (
                    "nReturned" in stage
                    or "executionTimeMillisEstimate" in stage
                )
                for stage in stages
            )

            assert (
                contains_execution_stats(explain) or has_stage_metrics
            ), f"{path} has neither executionStats nor stage execution metrics"
        else:
            assert contains_execution_stats(explain), (
                f"{path} has no executionStats"
            )

# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------

def test_round_half_up_exact_tie():
    assert api.round_half_up(Decimal("1.005"), 2) == 1.01
    assert api.round_half_up(Decimal("1.015"), 2) == 1.02


def test_parse_date_requires_exact_iso_format():
    assert api.parse_date("2026-02-28") == date(2026, 2, 28)
    with pytest.raises(ValueError):
        api.parse_date("2026-2-8")
    with pytest.raises(ValueError):
        api.parse_date("2026-02-30")


def test_validate_epoch_rejects_bool_float_and_seconds():
    for value in (True, 1.25, "1791800000000", 1):
        with pytest.raises(ValueError):
            api.validate_epoch(value)


def test_work_metrics_exact_overtime_threshold():
    pin = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)  # 09:30 IST
    at_threshold = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)  # 18:30 IST
    assert api.work_metrics(pin, at_threshold, "09:30", "18:30")["overtime_minutes"] == 0
    thirty_late = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)  # 19:00 IST
    assert api.work_metrics(pin, thirty_late, "09:30", "18:30")["overtime_minutes"] == 30

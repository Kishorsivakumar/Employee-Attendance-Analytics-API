import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.collection import Collection


# Load .env from the directory containing this Python file
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_FILE)
# Read MongoDB configuration from .env
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

# Fail early if configuration is missing
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError(
        f"Missing MONGO_URI or MONGO_DB in: {ENV_FILE}"
    )

# Create one reusable MongoDB client
client = MongoClient(MONGO_URI,serverSelectionTimeoutMS=3000,connectTimeoutMS=3000)
db = client[MONGO_DB]
employees: Collection = db.employees
attendance: Collection = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    attendance.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_late_date")


@app.on_event("startup")
def startup():
    client.admin.command("ping")
    print("MongoDB connected successfully")
    ensure_indexes()
    print("MongoDB indexes ensured")


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_ms(dt: datetime | None):
    if dt is None:
        return None
    return int(as_utc(dt).timestamp() * 1000)


def from_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    # Pydantic validates the public input before this helper is called.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def trunc_second(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def validate_epoch(value):
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch-milliseconds value")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError(f"must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}")
    return value


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first, last


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def mongo_round_half_up(expression, places=2):
    """MongoDB expression for half-up rounding of non-negative numeric values."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, factor]}, 0.5]}}, factor]}


def minutes_of(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def shift_datetimes(punch_in: datetime, shift_start: str, shift_end: str):
    local_in = as_utc(punch_in).astimezone(IST)
    start_min, end_min = minutes_of(shift_start), minutes_of(shift_end)
    overnight = end_min <= start_min
    # For overnight shifts, a punch before shift_end belongs to the prior shift date.
    shift_day = local_in.date()
    local_clock = local_in.hour * 60 + local_in.minute
    if overnight and local_clock < end_min:
        shift_day -= timedelta(days=1)
    start_local = datetime.combine(shift_day, time(start_min // 60, start_min % 60), IST)
    end_day = shift_day + (timedelta(days=1) if overnight else timedelta())
    end_local = datetime.combine(end_day, time(end_min // 60, end_min % 60), IST)
    return start_local.astimezone(UTC), end_local.astimezone(UTC), shift_day.isoformat()


def late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    start, _, _ = shift_datetimes(punch_in, shift_start, shift_end)
    elapsed = int((trunc_second(punch_in) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def work_metrics(punch_in: datetime | None, punch_out: datetime | None,
                 shift_start: str, shift_end: str):
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, shift_start, shift_end)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((trunc_second(punch_out) - trunc_second(punch_in)).total_seconds())
    hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
    _, end, _ = shift_datetimes(punch_in, shift_start, shift_end)
    overtime_elapsed = int((trunc_second(punch_out) - end).total_seconds())
    overtime = overtime_elapsed // 60 if overtime_elapsed >= 1800 else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": max(0, overtime), "half_day": hours < 4.5}


def record_out(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        d[key] = to_ms(d.get(key))
    d["history"] = d.get("history") or []
    for entry in d["history"]:
        entry["at"] = to_ms(entry.get("at"))
        changes = entry.get("changes") or {}
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = to_ms(changes[field].get("from"))
                changes[field]["to"] = to_ms(changes[field].get("to"))
        entry["changes"] = changes
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    return d


def employee_out(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d["created_at"] = to_ms(d["created_at"])
    return d


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class   EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch(v)


class PunchOutRequest(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch(v)


class RegularizeRequest(StrictModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch(v)



@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now = trunc_second(datetime.now(UTC))
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = [employee_out(d) for d in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "emp_code": 1, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = trunc_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    local = ts.astimezone(IST)
    _, _, att_date = shift_datetimes(ts, emp["shift_start"], emp["shift_end"])
    # R1 shift-start date for overnight shifts, otherwise local punch-in date.
    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = trunc_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    # Find most recent open punch at or before the requested instant; supports overnight shifts.
    doc = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None, "status": {"$in": PRESENCE}},
        sort=[("punch_in", DESCENDING)],
    )
    if not doc:
        # A record exists for this attendance date but its punch-in is later
        # than the requested punch-out: report an invalid time, not a missing record.
        future_punch = attendance.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$gt": ts},
            "status": {"$in": PRESENCE},
        }, sort=[("punch_in", ASCENDING)])
        if future_punch:
            raise HTTPException(422, "punched_at must be after punch_in")
        # If no open record exists, distinguish an already-closed record from no punch-in.
        prior = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "status": {"$in": PRESENCE}}, sort=[("punch_in", DESCENDING)])
        if prior and prior.get("punch_out") is not None:
            raise HTTPException(409, "record already punched out")
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    seconds = (ts - trunc_second(doc["punch_in"])).total_seconds()
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    metrics = work_metrics(doc["punch_in"], ts, emp["shift_start"], emp["shift_end"])
    result = attendance.update_one(
        {"emp_code": body.emp_code, "date": doc["date"], "punch_out": None},
        {"$set": {"punch_out": ts, **metrics}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record already punched out")
    updated = attendance.find_one({"emp_code": body.emp_code, "date": doc["date"]})
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    total = attendance.count_documents(q)
    docs = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    try:
        parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    key = {"emp_code": emp_code, "date": date}
    original = attendance.find_one(key)
    if not original:
        raise HTTPException(404, "attendance record not found")

    current = {k: original.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    status = body.status if body.status is not None else original.get("status", "PRESENT")
    pin = trunc_second(from_ms(body.punch_in)) if body.punch_in is not None else original.get("punch_in")
    pout = trunc_second(from_ms(body.punch_out)) if body.punch_out is not None else original.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None:
        local_day = pin.astimezone(IST).date().isoformat()
        _, _, calculated_date = shift_datetimes(pin, emp["shift_start"], emp["shift_end"])
        if calculated_date != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
    if pout is not None:
        if pin is None or pout <= pin or (pout - pin).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if status in ("ABSENT", "LEAVE"):
        metrics = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        metrics = work_metrics(pin, pout, emp["shift_start"], emp["shift_end"])
    final = {"status": status, "punch_in": pin, "punch_out": pout, **metrics}
    changes = {}
    for field, new in final.items():
        old = current.get(field)
        if field in ("punch_in", "punch_out"):
            old = trunc_second(old) if old else None
        if old != new:
            changes[field] = {"from": old, "to": new}
    # A PATCH containing only audit metadata is not an attendance correction.
    # Do not allow recomputed legacy metrics to turn an otherwise empty request
    # into a successful regularization.
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "request does not change the record")
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": trunc_second(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap prevents lost updates/history under concurrent PATCHes.
    result = attendance.update_one(
        {**key, "$and": [
            {"$or": [
                {"history": original.get("history", [])},
                {"history": {"$exists": False}} if "history" not in original else {"history": original.get("history", [])},
            ]},
        ], "status": original.get("status"),
         "punch_in": original.get("punch_in"), "punch_out": original.get("punch_out")},
        {"$set": final, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry")
    return record_out(attendance.find_one(key))


def present_expr():
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}


def date_range_strings(first: date, last: date):
    return first.isoformat(), last.isoformat()


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    effective = max(first, parse_date(emp["joined_on"]))
    working_days = sum(1 for n in range((last - effective).days + 1) if (effective + timedelta(days=n)).weekday() < 5) if effective <= last else 0
    start, end = date_range_strings(first, last)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    agg = list(attendance.aggregate(pipeline))
    a = agg[0] if agg else {}
    present = float(a.get("present_days", 0))
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": round_half_up(present),
            "leave_days": a.get("leave_days", 0), "late_count": a.get("late_count", 0),
            "total_late_minutes": a.get("total_late_minutes", 0), "total_overtime_minutes": a.get("total_overtime_minutes", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}]}}},
            {"$project": {"status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, mongo_round_half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    items = list(employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    start, end = from_date.isoformat(), to_date.isoformat()
    # Aggregate logs by date, then densify missing calendar dates in MongoDB.
    # Headcount is computed as a single date-range aggregation over employees; no per-day/per-employee queries.
    # MongoDB pipeline builds calendar rows and windowed rates. Date strings are converted only inside the pipeline.
    # Use a compact date-spine via $documents/$map where available to keep all gap-filling in MongoDB.
    spine_pipeline = [
        {"$documents": [{"start": start, "end": end}]},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, (to_date - from_date).days + 1]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start}}, "unit": "day", "amount": "$$i"}}}}}}}},
        {"$unwind": "$dates"},
        {"$project": {"date": "$dates"}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", employees.distinct("emp_code", {"department": department})]}]}}},
            {"$group": {
                "_id": None,
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                    0
                ]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                ]}}
            }},
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {"present_count": 0, "late_count": 0}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": "$stats.present_count", "late_count": "$stats.late_count",
                  "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_round_half_up({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_rates": {"$push": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": {"$let": {"vars": {"nonnull": {"$filter": {"input": "$_rates", "as": "r", "cond": {"$ne": ["$$r", None]}}}},
            "in": {"$cond": [{"$gt": [{"$size": "$$nonnull"}, 0]}, mongo_round_half_up({"$avg": "$$nonnull"}, 4), None]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    # Distinct employee codes are fetched once, not once per day. The aggregation does all daily grouping and windowing.
    codes = employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "department not found")
    spine_pipeline[4]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"][1]["$in"][1] = codes
    items = list(db.aggregate(spine_pipeline))
    return {"department": department, "items": items}



def explain_execution(endpoint: str, params: dict):
    """Explain the same main query/pipeline used by the corresponding endpoint."""
    if endpoint == "attendance_list":
        page = params.get("page", 1)
        page_size = params.get("page_size", 20)
        q = {}
        if params.get("emp_code") is not None:
            q["emp_code"] = params["emp_code"]
        if params.get("status") is not None:
            q["status"] = params["status"]
        if params.get("date_from") or params.get("date_to"):
            q["date"] = {}
            if params.get("date_from"):
                q["date"]["$gte"] = params["date_from"]
            if params.get("date_to"):
                q["date"]["$lte"] = params["date_to"]
        explanation = attendance.database.command(
            "explain",
            {"find": attendance.name, "filter": q,
             "sort": {"date": -1, "emp_code": 1},
             "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats",
        )
        return "attendance_logs", explanation

    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"):
            raise HTTPException(422, "emp_code and month are required")
        first, last = month_bounds(params["month"])
        start, end = date_range_strings(first, last)
        pipeline = [
            {"$match": {"emp_code": params["emp_code"], "date": {"$gte": start, "$lte": end}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                ]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]
        return "attendance_logs", attendance.database.command(
            "explain", {"aggregate": attendance.name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    if endpoint == "department_summary":
        if not params.get("month"):
            raise HTTPException(422, "month is required")
        first, last = month_bounds(params["month"])
        match = {"joined_on": {"$lte": last.isoformat()}}
        if params.get("department") is not None:
            match["department"] = params["department"]
        pipeline = [
            {"$match": match},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", first.isoformat()]},
                    {"$lte": ["$date", last.isoformat()]},
                ]}}},
                {"$project": {"status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1}},
            ], "as": "logs"}},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
                "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
                "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, mongo_round_half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2), None]},
                "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
            {"$sort": {"department": 1}},
        ]
        return "employees", employees.database.command(
            "explain", {"aggregate": employees.name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    if endpoint == "late_leaderboard":
        if not params.get("month"):
            raise HTTPException(422, "month is required")
        first, last = month_bounds(params["month"])
        pipeline = [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if params.get("department") is not None:
            pipeline.append({"$match": {"employee.department": params["department"]}})
        pipeline += [
            {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": params.get("limit", 10)}}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
        return "attendance_logs", attendance.database.command(
            "explain", {"aggregate": attendance.name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    if endpoint == "department_trend":
        if not all(params.get(k) for k in ("department", "from", "to")):
            raise HTTPException(422, "department, from and to are required")
        from_date = parse_date(params["from"])
        to_date = parse_date(params["to"])
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        codes = employees.distinct("emp_code", {"department": params["department"]})
        if not codes:
            raise HTTPException(404, "department not found")
        start, end = from_date.isoformat(), to_date.isoformat()
        pipeline = [
            {"$documents": [{"start": start, "end": end}]},
            {"$project": {"dates": {"$map": {"input": {"$range": [0, (to_date - from_date).days + 1]}, "as": "i",
                "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start}}, "unit": "day", "amount": "$$i"}}}}}}}},
            {"$unwind": "$dates"},
            {"$project": {"date": "$dates"}},
            {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", codes]}]}}},
                {"$group": {"_id": None,
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ], "as": "stats"}},
            {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {"present_count": 0, "late_count": 0}]}}},
            {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", params["department"]]}, {"$lte": ["$joined_on", "$$d"]}]}}},
                {"$count": "n"},
            ], "as": "hc"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": "$stats.present_count", "late_count": "$stats.late_count",
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_round_half_up({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4), None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_rates": {"$push": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$set": {"moving_avg_7d": {"$let": {"vars": {"nonnull": {"$filter": {"input": "$_rates", "as": "r", "cond": {"$ne": ["$$r", None]}}}},
                "in": {"$cond": [{"$gt": [{"$size": "$$nonnull"}, 0]}, mongo_round_half_up({"$avg": "$$nonnull"}, 4), None]}}}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
            {"$sort": {"date": 1}},
        ]
        return "attendance_logs", db.command(
            "explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
    raise HTTPException(422, "unsupported endpoint")

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    params = {"emp_code": emp_code, "month": month, "department": department, "limit": limit,
              "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None,
              "status": status, "from": from_date.isoformat() if from_date else None, "to": to_date.isoformat() if to_date else None,
              "page": page, "page_size": page_size}
    collection, explanation = explain_execution(endpoint, params)
    return {"endpoint": endpoint, "collection": collection, "explain": explanation}
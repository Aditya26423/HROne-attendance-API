"""
Employee Attendance & Analytics API (HROne engineering assignment).

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

All application code lives in this one file, as the assignment requires.
Sections: configuration -> rule helpers (R1-R5) -> serializers -> models ->
          query/pipeline builders -> endpoints -> explain endpoint.
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: every datetime read from MongoDB is timezone-aware (UTC) instead of naive.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
# name -> (collection, key pattern, unique)
INDEXES = {
    # employees
    "emp_code_uq": ("employees", [("emp_code", 1)], True),  # unique business key
    "dept_emp_code": ("employees", [("department", 1), ("emp_code", 1)], False),  # list by department
    "dept_joined_on": ("employees", [("department", 1), ("joined_on", 1)], False),  # headcount (R9), trend
    "joined_on": ("employees", [("joined_on", 1)], False),  # headcount without department filter
    # attendance_logs
    "emp_date_uq": ("attendance_logs", [("emp_code", 1), ("date", 1)], True),  # one record per day + race guard
    "date_emp": ("attendance_logs", [("date", -1), ("emp_code", 1)], False),  # list sort, leaderboard
    "status_date_emp": ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], False),  # status filter
    "emp_punch_in": ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], False),  # punch-out lookup
}

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if _indexes_ready:
            return
        for name, (coll, keys, unique) in INDEXES.items():
            db[coll].create_index(keys, name=name, unique=unique)
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # the app still starts; /health reports 503 until MongoDB answers
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def mongo_unavailable(_: Request, exc: ConnectionFailure):
    # Only connectivity problems become 503; real bugs still surface as 500 instead of being hidden.
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def bad_request(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body, for checks that Pydantic cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: tuple) -> Date:
    if not DATE_RE.fullmatch(value):
        raise bad_request(loc, "must be a date in YYYY-MM-DD format")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise bad_request(loc, "not a real calendar date")


def month_range(month: str, loc: tuple = ("query", "month")) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise bad_request(loc, "year must be >= 0001")
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def require(value: Any, name: str) -> Any:
    if value is None:
        raise bad_request(("query", name), "field required for this endpoint")
    return value


# --------------------------------------------------------------------------- #
# Time and rule helpers (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, UTC)


def dt_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)  # exact integer arithmetic, no float


def aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; overnight shifts push early-morning punches to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(rec_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `rec_date` (end is next day if overnight)."""
    day = Date.fromisoformat(rec_date)
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    return start, datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, rec_date: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 600 s after shift start; then floor minutes since shift start."""
    start, _ = shift_bounds(rec_date, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, rec_date: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, counted only if >= 30."""
    _, end = shift_bounds(rec_date, shift_start, shift_end)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def work_hours_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in integers: round-half-up(seconds / 36) = hours * 100. Avoids float and banker's rounding."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (2 * seconds + 36) // 72


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
           rec_date: str, shift_start: str, shift_end: str) -> dict:
    """All derived fields for a record in its final state (see the table in DATA_MODEL.md)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE and punch_in is not None:
        out["late_minutes"] = compute_late_minutes(punch_in, rec_date, shift_start, shift_end)
        if punch_out is not None:
            hundredths = work_hours_hundredths(punch_in, punch_out)
            out["work_hours"] = hundredths / 100
            out["overtime_minutes"] = compute_overtime(punch_out, rec_date, shift_start, shift_end)
            out["half_day"] = hundredths < 450  # R5: rounded work_hours < 4.50
    return out


def half_up(value: Any, places: int) -> float:
    """R8: round half up (MongoDB's $round and Python's round() are both banker's rounding)."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def working_days(start: str, end: str, joined_on: str) -> int:
    """R7: Mon-Fri between max(month start, joined_on) and month end, no holiday calendar."""
    day = max(Date.fromisoformat(start), Date.fromisoformat(joined_on))
    last = Date.fromisoformat(end)
    count = 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serializers (BSON -> API)
# --------------------------------------------------------------------------- #
def to_api(value: Any) -> Any:
    """Recursively turn datetimes into epoch milliseconds (also inside history[].changes)."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_api(doc.get("punch_in")),
        "punch_out": to_api(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),  # legacy documents may omit it
        "history": to_api(doc.get("history") or []),  # legacy documents may omit it
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": to_api(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
# strict=True: JSON floats ("1.5", 1783312500000.0), strings and booleans are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def real_date(cls, value: str) -> str:
        try:
            Date.fromisoformat(value)
        except ValueError:
            raise ValueError("not a real calendar date")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, data: Any):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared by the real endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
LIST_SORT = [("date", DESCENDING), ("emp_code", 1)]
LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def present_expr(weekdays_only: bool) -> dict:
    """Contribution of one record to present_days: 1, or 0.5 for a half day, 0 if not present (R5-R7)."""
    cond: dict = IS_PRESENT
    if weekdays_only:
        weekday = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
        cond = {"$and": [IS_PRESENT, weekday]}
    return {"$cond": [cond, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def totals_group() -> dict:
    """One $group stage that produces every sum the monthly and department summaries need."""
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": present_expr(True)},
        "late_count": {"$sum": LATE_FLAG},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "wh_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
        "wh_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
    }}


TOTAL_KEYS = ["present_days", "late_count", "total_late_minutes", "total_overtime_minutes",
              "leave_count", "on_duty_count", "wh_sum", "wh_n"]


def attendance_list_query(emp_code: Optional[str], date_from: Optional[str],
                          date_to: Optional[str], status: Optional[str]) -> tuple[dict, str]:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    hint = "emp_date_uq" if emp_code is not None else "status_date_emp" if status is not None else "date_emp"
    return query, hint


def check_list_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    low = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    high = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if low and high and low > high:
        raise bad_request(("query", "date_from"), "date_from must not be after date_to")


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> tuple[str, list, str]:
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # IXSCAN on emp_date_uq
        totals_group(),
    ]
    return "attendance_logs", pipeline, "emp_date_uq"


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> tuple[str, list, str]:
    """Starts from employees so that people with zero logs still count (R9)."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        # one small totals document per employee, computed inside the lookup via the emp_date_uq index
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, totals_group()],
            "as": "m",
        }},
        {"$addFields": {"m": {"$arrayElemAt": ["$m", 0]}}},  # missing when the employee has no logs
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": {"$ifNull": [f"$m.{k}", 0]}} for k in TOTAL_KEYS}}},
        {"$sort": {"_id": 1}},
    ]
    hint = "dept_joined_on" if department is not None else "joined_on"
    return "employees", pipeline, hint


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> tuple[str, list, str]:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},  # IXSCAN on date_emp
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": LATE_FLAG}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline, "date_emp"


def trend_range(date_from: str, date_to: str) -> tuple[Date, Date]:
    low = parse_date(date_from, ("query", "from"))
    high = parse_date(date_to, ("query", "to"))
    if high < low:
        raise bad_request(("query", "to"), "to must not be before from")
    if (high - low).days + 1 > 92:
        raise bad_request(("query", "to"), "range must not exceed 92 days")
    return low, high


def department_trend_pipeline(department: str, low: Date, high: Date) -> tuple[str, list, str]:
    start, end = low.isoformat(), high.isoformat()
    low_dt = datetime.combine(low, time.min, tzinfo=UTC)
    high_dt = datetime.combine(high, time.min, tzinfo=UTC)
    working = {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}
    window = {"documents": [-6, 0]}  # current row + up to 6 earlier rows inside the range
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}},
                         {"$project": {"_id": 0, "date": 1, "pc": present_expr(False), "late": LATE_FLAG}}],
            "as": "logs",
        }},
        # Employees without logs are kept (preserveNullAndEmptyArrays) and parked on day one with zero
        # contribution, so the stream is never empty and $densify always has something to work from.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"$ifNull": ["$logs.date", start]},
                    "present_count": {"$sum": {"$ifNull": ["$logs.pc", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$logs.late", 0]}}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"day": 1}},
        # gap filling: one document per calendar day; upper bound is exclusive, so go one day past `to`
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
                                                "bounds": [low_dt, high_dt + timedelta(days=1)]}}},
        {"$match": {"day": {"$lte": high_dt}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # R9 headcount: employees of the department with joined_on <= this day
        {"$lookup": {
            "from": "employees", "let": {"d": "$date"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", {"$literal": department}]},
                                                         {"$lte": ["$joined_on", "$$d"]}]}}},
                         {"$count": "n"}],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"has_rate": {"$cond": [working, 1, 0]}}},
        # rate in integer ten-thousandths, half-up: floor((10000*P2 + hc) / (2*hc)) where P2 = 2*present_count
        {"$addFields": {"rate_e4": {"$cond": [
            {"$eq": ["$has_rate", 1]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [10000, {"$multiply": ["$present_count", 2]}]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "w_sum": {"$sum": "$rate_e4", "window": window},  # $sum ignores the nulls
            "w_n": {"$sum": "$has_rate", "window": window}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$has_rate", 1]}, {"$divide": ["$rate_e4", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$w_n", 0]},
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$w_sum"]}, "$w_n"]},
                                                     {"$multiply": [2, "$w_n"]}]}}, 10000]},
                None]},
        }},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, "dept_joined_on"


# --------------------------------------------------------------------------- #
# Endpoints: system and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not available")
    try:
        ensure_indexes()  # covers the case where MongoDB was not up yet at startup
    except PyMongoError as exc:
        log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior find_one
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1, le=1_000_000_000),
                   page_size: int = Query(20, ge=1, le=100)):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)  # counts the filtered set (R10)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(body.status, ts, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): of N parallel requests exactly one wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record whose punch_in is at or before the punch-out; works across midnight
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                      sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = aware(rec["punch_in"])
    seconds = (ts - punch_in_dt).total_seconds()
    if seconds <= 0:
        raise bad_request(("body", "punched_at"), "punched_at must be after the punch-in")
    if seconds > 86400:
        raise bad_request(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")
    derived = derive(rec["status"], punch_in_dt, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    # Atomic compare-and-set: only succeeds while the record is still open and unchanged since we read it.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or modified concurrently")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1, le=1_000_000_000),
                    page_size: int = Query(20, ge=1, le=100)):
    check_list_dates(date_from, date_to)
    query, hint = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query, hint=hint)
    docs = db.attendance_logs.find(query, hint=hint, sort=LIST_SORT, skip=(page - 1) * page_size, limit=page_size)
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]

    # 1. Final state after applying the request (omitted fields stay as they are).
    old_in, old_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise bad_request(("body",), "punch_in/punch_out cannot be supplied with status ABSENT or LEAVE")
        new_in = new_out = None  # ABSENT / LEAVE clear both punch times
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise bad_request(("body", "punch_in"), "a presence status requires a punch_in")
        if attendance_date(new_in, shift_start, shift_end) != date:
            raise bad_request(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None:
            seconds = (new_out - new_in).total_seconds()
            if seconds <= 0 or seconds > 86400:
                raise bad_request(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    # 2. Recompute derived fields, then diff old vs new: only what really changed goes into history.
    new_vals = {"status": status, "punch_in": new_in, "punch_out": new_out,
                **derive(status, new_in, new_out, date, shift_start, shift_end)}
    old_vals = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {f: {"from": old_vals[f], "to": new_vals[f]} for f in CHANGE_FIELDS if old_vals[f] != new_vals[f]}
    if not changes:
        raise bad_request(("body",), "the request changes nothing")
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 3. Optimistic concurrency: write only if the record is exactly as we read it (same punches, status and
    #    history length). Otherwise someone else corrected it meanwhile and the loser gets 409 instead of
    #    silently overwriting or losing a history entry.
    guard: dict = {"emp_code": emp_code, "date": date, "status": rec["status"],
                   "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    history_len = len(rec.get("history") or [])
    if history_len:
        guard["history"] = {"$size": history_len}
        update: dict = {"$set": new_vals, "$push": {"history": entry}}
    else:  # history is missing, null or empty
        guard["$or"] = [{"history": None}, {"history": {"$size": 0}}]
        update = {"$set": {**new_vals, "history": [entry]}}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Endpoints: analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    start, end = month_range(month)
    emp = get_employee(emp_code)
    coll, pipeline, hint = employee_monthly_pipeline(emp_code, start, end)
    rows = list(db[coll].aggregate(pipeline, hint=hint))
    totals = rows[0] if rows else {}
    present = float(totals.get("present_days", 0))
    days = working_days(start, end, emp["joined_on"])
    pct = None if days == 0 else half_up(Decimal(str(present)) * 100 / days, 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": days,
        "present_days": half_up(present, 2),
        "leave_days": int(totals.get("leave_count", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    start, end = month_range(month)
    coll, pipeline, hint = department_summary_pipeline(start, end, department)
    items = []
    for row in db[coll].aggregate(pipeline, hint=hint):
        n = int(row["wh_n"])
        avg = None if n == 0 else half_up(Decimal(str(round(float(row["wh_sum"]), 6))) / n, 2)
        items.append({
            "department": row["_id"], "headcount": int(row["headcount"]),
            "present_days": half_up(row["present_days"], 2), "avg_work_hours": avg,
            "late_count": int(row["late_count"]), "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]), "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_RE),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_range(month)
    coll, pipeline, hint = late_leaderboard_pipeline(start, end, limit, department)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
             for r in db[coll].aggregate(pipeline, hint=hint)]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    low, high = trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipeline, hint = department_trend_pipeline(department, low, high)
    items = [{
        "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
        "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
        "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"],
    } for r in db[coll].aggregate(pipeline, hint=hint)]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoint: explain (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain('executionStats') on the same query/pipeline (and hint) the real endpoint builds."""
    if endpoint == "attendance_list":
        check_list_dates(date_from, date_to)
        query, hint = attendance_list_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command: dict = {"find": collection, "filter": query, "sort": dict(LIST_SORT),
                         "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint == "employee_monthly":
            start, end = month_range(require(month, "month"))
            collection, pipeline, hint = employee_monthly_pipeline(require(emp_code, "emp_code"), start, end)
        elif endpoint == "department_summary":
            start, end = month_range(require(month, "month"))
            collection, pipeline, hint = department_summary_pipeline(start, end, department)
        elif endpoint == "late_leaderboard":
            start, end = month_range(require(month, "month"))
            collection, pipeline, hint = late_leaderboard_pipeline(start, end, limit, department)
        else:  # department_trend
            low, high = trend_range(require(from_, "from"), require(to, "to"))
            collection, pipeline, hint = department_trend_pipeline(require(department, "department"), low, high)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    raw = db.command("explain", command, verbosity="executionStats")
    # BSON -> plain JSON (dates, Int64, ...) so the raw explain document can be returned as is
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}

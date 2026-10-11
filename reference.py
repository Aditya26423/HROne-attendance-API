"""
Pure-Python reference implementation of the analytics rules (R5-R9), used ONLY by the tests.

It is written straight from the prose in openapi.yaml with Decimal arithmetic and plain loops, so it shares no
code with the MongoDB pipelines in app/main.py. If the two disagree, one of them is wrong.
"""
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


def hu(value, places):
    """Round half up. `value` may be int, float, str or Decimal."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_bounds(month):
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def in_month(log, month):
    return log["date"].startswith(month)


def weekday(day):
    return date.fromisoformat(day).weekday() < 5


def present_value(log, weekdays_only):
    if log["status"] not in PRESENT_STATUSES:
        return Decimal(0)
    if weekdays_only and not weekday(log["date"]):
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def working_days(month, joined_on):
    first, last = month_bounds(month)
    day = max(first, date.fromisoformat(joined_on))
    count = 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def monthly(employees, logs, emp_code, month):
    emp = next(e for e in employees if e["emp_code"] == emp_code)
    mine = [l for l in logs if l["emp_code"] == emp_code and in_month(l, month)]
    present = sum((present_value(l, True) for l in mine), Decimal(0))
    days = working_days(month, emp["joined_on"])
    return {
        "emp_code": emp_code, "month": month, "working_days": days, "present_days": hu(present, 2),
        "leave_days": sum(l["status"] == "LEAVE" for l in mine),
        "late_count": sum((l.get("late_minutes") or 0) > 0 for l in mine),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": None if days == 0 else hu(present / days * 100, 2),
    }


def department_summary(employees, logs, month, department=None):
    _, last = month_bounds(month)
    by_code = {e["emp_code"]: e for e in employees}
    out = []
    for dept in sorted({e["department"] for e in employees}):
        if department is not None and dept != department:
            continue
        members = [e for e in employees if e["department"] == dept and e["joined_on"] <= last.isoformat()]
        if not members:
            continue
        codes = {e["emp_code"] for e in members}
        mine = [l for l in logs if l["emp_code"] in codes and in_month(l, month) and l["emp_code"] in by_code]
        hours = [Decimal(str(l["work_hours"])) for l in mine
                 if l["status"] in PRESENT_STATUSES and l.get("work_hours") is not None]
        out.append({
            "department": dept, "headcount": len(members),
            "present_days": hu(sum((present_value(l, True) for l in mine), Decimal(0)), 2),
            "avg_work_hours": hu(sum(hours) / len(hours), 2) if hours else None,
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in mine),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
            "leave_count": sum(l["status"] == "LEAVE" for l in mine),
            "on_duty_count": sum(l["status"] == "ON_DUTY" for l in mine),
        })
    return {"month": month, "items": out}


def leaderboard(employees, logs, month, limit=10, department=None):
    by_code = {e["emp_code"]: e for e in employees}
    totals = {}
    for l in logs:
        if not in_month(l, month) or l["emp_code"] not in by_code:
            continue
        if department is not None and by_code[l["emp_code"]]["department"] != department:
            continue
        t = totals.setdefault(l["emp_code"], [0, 0])
        t[0] += l.get("late_minutes") or 0
        t[1] += (l.get("late_minutes") or 0) > 0
    rows = sorted(((code, t[0], t[1]) for code, t in totals.items() if t[0] > 0), key=lambda r: (-r[1], r[0]))
    items = []
    for code, total, count in rows:
        rank = 1 + sum(1 for r in rows if r[1] > total)  # standard competition ranking
        if rank <= limit:
            e = by_code[code]
            items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                          "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": items}


def trend(employees, logs, department, d_from, d_to):
    members = [e for e in employees if e["department"] == department]
    codes = {e["emp_code"] for e in members}
    rows, rates = [], []
    day = date.fromisoformat(d_from)
    while day <= date.fromisoformat(d_to):
        iso = day.isoformat()
        working = day.weekday() < 5
        headcount = sum(e["joined_on"] <= iso for e in members)
        todays = [l for l in logs if l["emp_code"] in codes and l["date"] == iso]
        present = sum((present_value(l, False) for l in todays), Decimal(0))
        rate = hu(present / headcount, 4) if working and headcount > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]  # current row + 6 before it, inside the range only
        moving = hu(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None
        rows.append({"date": iso, "is_working_day": working, "headcount": headcount, "present_count": float(present),
                     "late_count": sum((l.get("late_minutes") or 0) > 0 for l in todays),
                     "attendance_rate": rate, "moving_avg_7d": moving})
        day += timedelta(days=1)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Test data
# --------------------------------------------------------------------------- #
def utc(day, hh, mm, ist=True):
    """BSON-style aware datetime for an IST wall-clock time on `day` (YYYY-MM-DD)."""
    y, m, d = map(int, day.split("-"))
    base = datetime(y, m, d, hh, mm, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return base.astimezone(timezone.utc)


def make_log(emp_code, day, status="PRESENT", late=0, overtime=0, hours=8.0, half_day=False, open_=False, **extra):
    """A seeded attendance document with explicit stored derived values (what the grader's data looks like)."""
    present = status in PRESENT_STATUSES
    doc = {"emp_code": emp_code, "date": day, "status": status,
           "punch_in": utc(day, 9, 30) if present else None,
           "punch_out": utc(day, 18, 30) if present and not open_ else None,
           "work_hours": None if (not present or open_) else hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day, "history": []}
    doc.update(extra)
    return doc


def make_employee(code, dept, joined_on="2026-01-05", name=None, start="09:30", end="18:30"):
    return {"emp_code": code, "name": name or f"Name {code}", "email": f"{code.lower()}@example.com",
            "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined_on,
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)}


def random_dataset(seed=7, month="2026-07"):
    """~300 employees-days of messy data: ties, weekends, joiners, no-log employees, open records, half days."""
    rng = random.Random(seed)
    depts = ["Engineering", "Sales", "Support", "Ops"]
    employees = []
    for i in range(1, 25):
        joined = rng.choice(["2026-01-05", "2026-03-10", "2026-07-08", "2026-07-20", "2026-08-03"])
        employees.append(make_employee(f"EMP{i:04d}", depts[i % 4], joined))
    logs = []
    first, last = month_bounds(month)
    for e in employees[:-3]:  # the last three employees never have any logs
        day = first
        while day <= last:
            if rng.random() < 0.55:
                iso = day.isoformat()
                status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [8, 3, 2, 1, 1])[0]
                present = status in PRESENT_STATUSES
                half = present and rng.random() < 0.15
                logs.append(make_log(
                    e["emp_code"], iso, status,
                    late=rng.choice([0, 0, 11, 15, 30, 45]) if present else 0,
                    overtime=rng.choice([0, 0, 30, 45]) if present else 0,
                    hours=round(rng.uniform(1, 4.4) if half else rng.uniform(5, 10.5), 2),
                    half_day=half, open_=present and rng.random() < 0.05))
            day += timedelta(days=1)
    logs.append(make_log("EMP9999", f"{month}-10", late=500))  # log of an employee that does not exist
    return employees, logs

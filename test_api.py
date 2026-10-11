"""API tests against a real MongoDB (see conftest.py). Each test starts from empty collections."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tests import reference as ref

IST = timezone(timedelta(hours=5, minutes=30))


def ms(y, mo, d, h, mi, s=0, extra_ms=0):
    """Epoch milliseconds of an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + extra_ms


def add_emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05", **kw}
    return api.post("/employees", json=body)


def seed(m, employees=(), logs=()):
    """Insert documents directly, the way the grader seeds its dataset."""
    if employees:
        m.db.employees.insert_many(copy.deepcopy(list(employees)))
    if logs:
        m.db.attendance_logs.insert_many(copy.deepcopy(list(logs)))


def punch_in(api, code="EMP0001", at=None, **kw):
    body = {"emp_code": code, **kw}
    if at is not None:
        body["punched_at"] = at
    return api.post("/attendance/punch-in", json=body)


def punch_out(api, code="EMP0001", at=None):
    body = {"emp_code": code}
    if at is not None:
        body["punched_at"] = at
    return api.post("/attendance/punch-out", json=body)


def patch(api, code, day, **body):
    return api.patch(f"/attendance/{code}/{day}", json={"reason": "valid reason", "regularized_by": "hr.admin", **body})


# --------------------------------------------------------------------------- #
# health, startup, employees
# --------------------------------------------------------------------------- #
def test_health_ok_and_indexes_exist(api, app_module):
    assert api.get("/health").json() == {"status": "ok"}
    names = {i["name"] for c in ("employees", "attendance_logs") for i in app_module.db[c].list_indexes()}
    assert {"emp_code_uq", "emp_date_uq", "date_emp", "status_date_emp", "emp_punch_in"} <= names
    app_module._indexes_ready = False
    app_module.ensure_indexes()  # idempotent: a second run must not fail


def test_create_employee_and_validation(api):
    r = add_emp(api)
    assert r.status_code == 201
    body = r.json()
    assert "_id" not in body and body["shift_start"] == "09:30" and isinstance(body["created_at"], int)
    assert add_emp(api).status_code == 409
    for bad in [{"emp_code": "E1"}, {"email": "nope"}, {"shift_start": "25:00"}, {"shift_end": "09:30"},
                {"joined_on": "2026-02-30"}, {"name": ""}, {"department": "x" * 51}]:
        code = bad.get("emp_code", "EMP0009")
        assert add_emp(api, **{**bad, "emp_code": code}).status_code == 422, bad
    assert api.post("/employees", json={"emp_code": "EMP0010"}).status_code == 422


def test_concurrent_duplicate_employee_creates(api):
    with ThreadPoolExecutor(12) as pool:
        codes = list(pool.map(lambda _: add_emp(api, "EMP0042").status_code, range(12)))
    assert sorted(codes) == [201] + [409] * 11


def test_list_employees_filter_sort_pagination(api):
    for i in range(1, 6):
        add_emp(api, f"EMP{i:04d}", department="Eng" if i % 2 else "Ops")
    page = api.get("/employees?page=2&page_size=2").json()
    assert [e["emp_code"] for e in page["items"]] == ["EMP0003", "EMP0004"] and page["total"] == 5
    assert api.get("/employees?department=Eng").json()["total"] == 3  # total follows the filter
    for q in ("page=0", "page_size=0", "page_size=101"):
        assert api.get("/employees?" + q).status_code == 422


# --------------------------------------------------------------------------- #
# punch-in / punch-out
# --------------------------------------------------------------------------- #
def test_punch_in_success_and_rules(api):
    add_emp(api)
    r = punch_in(api, at=ms(2026, 7, 6, 9, 40, 1, 900))
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["punch_in"] % 1000 == 0  # truncated to seconds
    assert j["status"] == "PRESENT" and j["history"] == [] and j["work_hours"] is None and "id" not in j
    add_emp(api, "EMP0002")
    assert punch_in(api, "EMP0002", at=ms(2026, 7, 6, 9, 40), status="WFH").json()["late_minutes"] == 0


def test_punch_in_errors(api):
    add_emp(api)
    assert punch_in(api, "NOPE").status_code == 404
    for bad in ({"punched_at": 1783312500}, {"punched_at": 1783312500000.5}, {"punched_at": "1783312500000"},
                {"punched_at": 0}, {"punched_at": True}, {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "FOO"}):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad}).status_code == 422, bad
    assert punch_in(api, at=ms(2026, 7, 6, 9, 30)).status_code == 201
    assert punch_in(api, at=ms(2026, 7, 6, 15, 0)).status_code == 409  # same IST date


def test_punch_in_overnight_shift_date(api):
    add_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    j = punch_in(api, "EMP0005", at=ms(2026, 7, 7, 2, 0)).json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 240


def test_concurrent_punch_in_exactly_one_201(api):
    add_emp(api)
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: punch_in(api, at=ms(2026, 7, 6, 9, 30)).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_punch_out_success_and_errors(api):
    add_emp(api)
    assert punch_out(api, "NOPE").status_code == 404
    assert punch_out(api, at=ms(2026, 7, 6, 18, 0)).status_code == 404  # no punch-in at all
    punch_in(api, at=ms(2026, 7, 6, 10, 5))
    assert punch_out(api, at=ms(2026, 7, 6, 10, 5)).status_code == 422       # equal to punch_in
    assert punch_out(api, at=ms(2026, 7, 7, 10, 5, 1)).status_code == 422    # more than 24 h later
    r = punch_out(api, at=ms(2026, 7, 6, 19, 10))
    assert r.status_code == 200
    j = r.json()
    assert (j["work_hours"], j["late_minutes"], j["overtime_minutes"], j["half_day"]) == (9.08, 35, 40, False)
    assert punch_out(api, at=ms(2026, 7, 6, 20, 0)).status_code == 409       # already closed


def test_punch_out_across_midnight_overnight_shift(api):
    add_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    punch_in(api, "EMP0005", at=ms(2026, 7, 6, 21, 55))
    j = punch_out(api, "EMP0005", at=ms(2026, 7, 7, 6, 40)).json()
    assert (j["date"], j["work_hours"], j["overtime_minutes"], j["half_day"]) == ("2026-07-06", 8.75, 40, False)


def test_half_day_punch_out(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 9, 30))
    j = punch_out(api, at=ms(2026, 7, 6, 13, 0)).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True


def test_concurrent_punch_out_exactly_one_200(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 9, 30))
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: punch_out(api, at=ms(2026, 7, 6, 18, 30)).status_code, range(16)))
    assert sorted(codes) == [200] + [409] * 15


# --------------------------------------------------------------------------- #
# regularization (PATCH)
# --------------------------------------------------------------------------- #
def closed_day(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 10, 5))
    punch_out(api, at=ms(2026, 7, 6, 19, 10))


def test_patch_recomputes_and_appends_history(api):
    closed_day(api)
    r = patch(api, "EMP0001", "2026-07-06", punch_in=ms(2026, 7, 6, 9, 28), late_minutes=99)  # derived: ignored
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.7 and len(j["history"]) == 1
    entry = j["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "valid reason" and isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # only what really changed
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["punch_in"] == {"from": ms(2026, 7, 6, 10, 5), "to": ms(2026, 7, 6, 9, 28)}

    j = patch(api, "EMP0001", "2026-07-06", status="ABSENT").json()  # second correction appends
    assert len(j["history"]) == 2 and j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None
    assert j["history"][0] == entry  # the first entry is untouched
    assert j["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "ABSENT"}
    assert patch(api, "EMP0001", "2026-07-06", status="WFH").status_code == 422  # presence needs a punch_in
    assert patch(api, "EMP0001", "2026-07-06", status="WFH", punch_in=ms(2026, 7, 6, 9, 0)).status_code == 200


def test_patch_rejections(api):
    closed_day(api)
    d = "2026-07-06"
    assert patch(api, "EMP0001", d).status_code == 422                                          # changes nothing
    assert patch(api, "EMP0001", d, punch_in=ms(2026, 7, 6, 10, 5)).status_code == 422          # same value
    assert patch(api, "EMP0001", d, status="LEAVE", punch_in=ms(2026, 7, 6, 9, 0)).status_code == 422
    assert patch(api, "EMP0001", d, punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422           # other date
    assert patch(api, "EMP0001", d, punch_out=ms(2026, 7, 6, 10, 5)).status_code == 422         # <= punch_in
    assert patch(api, "EMP0001", d, punch_out=ms(2026, 7, 7, 10, 6)).status_code == 422         # > 24 h
    assert patch(api, "EMP0001", d, punch_in=ms(2026, 7, 6, 20, 0)).status_code == 422         # after punch_out
    assert patch(api, "EMP0001", d, punch_in=1783312500).status_code == 422                    # seconds
    assert patch(api, "EMP0001", d, status="BOGUS").status_code == 422
    assert patch(api, "EMP0001", d, reason="abc", punch_in=ms(2026, 7, 6, 9, 0)).status_code == 422
    assert api.patch(f"/attendance/EMP0001/{d}", json={"punch_in": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert patch(api, "EMP0001", "2026-07-09", punch_in=ms(2026, 7, 9, 9, 0)).status_code == 404  # no record
    assert patch(api, "NOPE", d, punch_in=ms(2026, 7, 6, 9, 0)).status_code == 404
    assert patch(api, "EMP0001", "2026-02-30", punch_in=ms(2026, 7, 6, 9, 0)).status_code == 422


def test_concurrent_patches_never_lose_history(api):
    closed_day(api)
    minutes = range(1, 9)
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda k: patch(api, "EMP0001", "2026-07-06",
                                              punch_in=ms(2026, 7, 6, 9, k)).status_code, minutes))
    assert set(codes) <= {200, 409} and codes.count(200) >= 1
    history = api.get("/attendance?emp_code=EMP0001").json()["items"][0]["history"]
    assert len(history) == codes.count(200)                                   # every 200 left exactly one entry
    for earlier, later in zip(history, history[1:]):                          # entries chain: from == previous to
        assert later["changes"]["punch_in"]["from"] == earlier["changes"]["punch_in"]["to"]


# --------------------------------------------------------------------------- #
# GET /attendance
# --------------------------------------------------------------------------- #
def test_list_attendance_sort_filters_pagination(api, app_module):
    emps = [ref.make_employee("EMP0001", "Eng"), ref.make_employee("EMP0002", "Eng")]
    logs = [ref.make_log(c, f"2026-07-0{d}") for c in ("EMP0002", "EMP0001") for d in (6, 7, 8)]
    logs.append({"emp_code": "EMP0002", "date": "2026-07-09", "status": "LEAVE", "punch_in": None,
                 "punch_out": None, "work_hours": None, "late_minutes": 0})  # legacy document: no half_day/history
    seed(app_module, emps, logs)
    j = api.get("/attendance?page_size=3").json()
    assert j["total"] == 7
    assert [(i["date"], i["emp_code"]) for i in j["items"]] == [
        ("2026-07-09", "EMP0002"), ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002")]
    assert j["items"][0]["half_day"] is False and j["items"][0]["history"] == [] and "id" not in j["items"][0]
    assert api.get("/attendance?emp_code=EMP0001").json()["total"] == 3
    assert api.get("/attendance?date_from=2026-07-07&date_to=2026-07-07").json()["total"] == 2  # inclusive bounds
    assert api.get("/attendance?status=LEAVE").json()["total"] == 1
    assert api.get("/attendance?emp_code=NOBODY").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert api.get("/attendance?emp_code=EMP0001&page=2&page_size=2").json()["items"][0]["date"] == "2026-07-06"
    for q in ("date_from=2026-07-09&date_to=2026-07-01", "date_from=2026-13-01", "date_from=junk", "status=FOO",
              "page=0", "page_size=101"):
        assert api.get("/attendance?" + q).status_code == 422, q


# --------------------------------------------------------------------------- #
# analytics: hand-computed cases
# --------------------------------------------------------------------------- #
def awkward_dataset():
    employees = [
        ref.make_employee("EMP0001", "Alpha", "2026-01-05"),
        ref.make_employee("EMP0002", "Alpha", "2026-07-13"),   # mid-month joiner
        ref.make_employee("EMP0003", "Alpha", "2026-08-03"),   # joins after July
        ref.make_employee("EMP0004", "Alpha", "2026-01-05"),   # no logs at all
    ]
    logs = [
        ref.make_log("EMP0001", "2026-07-06", hours=9.0),
        ref.make_log("EMP0001", "2026-07-07", late=20, hours=4.0, half_day=True),
        ref.make_log("EMP0001", "2026-07-08", "WFH", late=35, overtime=45, hours=8.0),
        ref.make_log("EMP0001", "2026-07-09", "LEAVE"),
        ref.make_log("EMP0001", "2026-07-10", "ABSENT"),
        ref.make_log("EMP0001", "2026-07-11", late=12, overtime=60, hours=8.0),      # Saturday
        ref.make_log("EMP0001", "2026-07-13", "ON_DUTY", open_=True),                # open record
        ref.make_log("EMP0002", "2026-07-14", hours=8.0),
    ]
    return employees, logs


def test_employee_monthly_hand_computed(api, app_module):
    seed(app_module, *awkward_dataset())
    j = api.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert j == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 3.5,
                 "leave_days": 1, "late_count": 3, "total_late_minutes": 67, "total_overtime_minutes": 105,
                 "attendance_pct": 15.22}   # Saturday adds to late/overtime but not to present_days
    j = api.get("/analytics/employees/EMP0002/monthly?month=2026-07").json()
    assert (j["working_days"], j["present_days"], j["attendance_pct"]) == (15, 1.0, 6.67)  # counted from joined_on
    j = api.get("/analytics/employees/EMP0003/monthly?month=2026-07").json()
    assert (j["working_days"], j["present_days"], j["attendance_pct"]) == (0, 0, None)
    j = api.get("/analytics/employees/EMP0004/monthly?month=2026-07").json()   # no logs: zeros, not an error
    assert (j["working_days"], j["present_days"], j["late_count"], j["attendance_pct"]) == (23, 0, 0, 0.0)
    assert api.get("/analytics/employees/NOPE/monthly?month=2026-07").status_code == 404
    for month in ("2026-13", "2026-7", "202607", "", "0000-05"):
        assert api.get(f"/analytics/employees/EMP0001/monthly?month={month}").status_code == 422, month
    assert api.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_department_summary_headcount_rules(api, app_module):
    seed(app_module, *awkward_dataset())
    items = api.get("/analytics/departments/summary?month=2026-07").json()["items"]
    assert len(items) == 1 and items[0]["department"] == "Alpha"
    assert items[0]["headcount"] == 3          # EMP0004 has no logs but counts; EMP0003 has not joined yet
    assert items[0]["present_days"] == 4.5 and items[0]["on_duty_count"] == 1 and items[0]["leave_count"] == 1
    assert items[0]["late_count"] == 3 and items[0]["total_late_minutes"] == 67
    june = api.get("/analytics/departments/summary?month=2026-06").json()["items"]
    assert june[0]["headcount"] == 2 and june[0]["present_days"] == 0 and june[0]["avg_work_hours"] is None
    assert api.get("/analytics/departments/summary?month=2026-12&department=Nope").json()["items"] == []


def test_late_leaderboard_ties_and_limit_after_ranking(api, app_module):
    emps = [ref.make_employee(f"EMP{i:04d}", "Beta") for i in range(1, 7)] + [ref.make_employee("EMP0007", "Gamma")]
    totals = {"EMP0001": 100, "EMP0002": 80, "EMP0003": 80, "EMP0004": 50, "EMP0005": 0, "EMP0007": 200}
    logs = [ref.make_log(c, "2026-07-06", late=t) for c, t in totals.items()]
    logs.append(ref.make_log("EMP0003", "2026-07-07", late=0))
    logs.append(ref.make_log("EMP9999", "2026-07-06", late=999))   # employee does not exist: ignored
    seed(app_module, emps, logs)

    def board(**q):
        qs = "&".join(f"{k}={v}" for k, v in {"month": "2026-07", **q}.items())
        return [(r["rank"], r["emp_code"]) for r in api.get(f"/analytics/leaderboard/late?{qs}").json()["items"]]

    assert board(department="Beta", limit=10) == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003"), (4, "EMP0004")]
    assert board(department="Beta", limit=1) == [(1, "EMP0001")]
    assert board(department="Beta", limit=2) == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003")]  # 3 rows for limit 2
    assert board(department="Beta", limit=3) == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003")]  # rank 4 > 3
    assert board(department="Beta", limit=4)[-1] == (4, "EMP0004")
    assert board(limit=2) == [(1, "EMP0007"), (2, "EMP0001")]            # ranking across all departments
    assert board(month="2026-08") == []
    row = api.get("/analytics/leaderboard/late?month=2026-07&department=Beta").json()["items"][0]
    assert row == {"rank": 1, "emp_code": "EMP0001", "name": "Name EMP0001", "department": "Beta",
                   "total_late_minutes": 100, "late_count": 1}
    for q in ("month=2026-07&limit=0", "month=2026-07&limit=51", "month=2026-13", "limit=5"):
        assert api.get("/analytics/leaderboard/late?" + q).status_code == 422, q


def trend_dataset():
    emps = [ref.make_employee(f"EMP{i:04d}", "Beta") for i in (1, 2, 3)] + [
        ref.make_employee("EMP0004", "Beta", "2026-07-08"), ref.make_employee("EMP0010", "Other")]
    logs = [
        ref.make_log("EMP0001", "2026-07-06"), ref.make_log("EMP0002", "2026-07-06", "WFH"),
        ref.make_log("EMP0003", "2026-07-06", half_day=True, hours=4.0),
        ref.make_log("EMP0001", "2026-07-08"), ref.make_log("EMP0004", "2026-07-08"),
        ref.make_log("EMP0001", "2026-07-10", late=15),
        ref.make_log("EMP0002", "2026-07-11"),                      # Saturday record
        ref.make_log("EMP0010", "2026-07-07"),                      # other department: not counted
    ]
    return emps, logs


def test_department_trend_hand_computed(api, app_module):
    seed(app_module, *trend_dataset())
    j = api.get("/analytics/departments/Beta/trend?from=2026-07-06&to=2026-07-12").json()
    assert j["department"] == "Beta" and [r["date"] for r in j["items"]] == [f"2026-07-{d:02d}" for d in range(6, 13)]
    rows = j["items"]
    assert [r["is_working_day"] for r in rows] == [True] * 5 + [False] * 2
    assert [r["headcount"] for r in rows] == [3, 3, 4, 4, 4, 4, 4]                    # joiner counts from 07-08
    assert [r["present_count"] for r in rows] == [2.5, 0, 2, 0, 1, 1, 0]               # half day = 0.5, weekend counted
    assert [r["late_count"] for r in rows] == [0, 0, 0, 0, 1, 0, 0]
    assert [r["attendance_rate"] for r in rows] == [0.8333, 0.0, 0.5, 0.0, 0.25, None, None]  # empty weekday = 0.0
    assert [r["moving_avg_7d"] for r in rows] == [0.8333, 0.4167, 0.4444, 0.3333, 0.3167, 0.3167, 0.3167]
    assert j == ref.trend(*trend_dataset(), "Beta", "2026-07-06", "2026-07-12")


def test_department_trend_without_any_logs_still_returns_every_day(api, app_module):
    seed(app_module, *trend_dataset())
    items = api.get("/analytics/departments/Beta/trend?from=2026-09-01&to=2026-09-07").json()["items"]
    assert len(items) == 7 and all(r["present_count"] == 0 for r in items)
    assert [r["attendance_rate"] for r in items] == [0.0] * 5 + [None, None]


def test_department_trend_validation(api, app_module):
    seed(app_module, *trend_dataset())
    url = "/analytics/departments/Beta/trend"
    assert api.get(f"{url}?from=2026-07-10&to=2026-07-09").status_code == 422
    assert api.get(f"{url}?from=2026-07-01&to=2026-09-30").json()["items"].__len__() == 92   # 92 days: allowed
    assert api.get(f"{url}?from=2026-07-01&to=2026-10-01").status_code == 422                # 93 days: too long
    assert api.get(f"{url}?from=2026-07-01").status_code == 422
    assert api.get(f"{url}?from=junk&to=2026-07-02").status_code == 422
    assert api.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02").status_code == 404


# --------------------------------------------------------------------------- #
# analytics: messy generated data compared with the independent reference model
# --------------------------------------------------------------------------- #
@pytest.fixture()
def messy(api, app_module):
    employees, logs = ref.random_dataset()
    seed(app_module, employees, logs)
    return employees, logs


def test_monthly_matches_reference_for_every_employee(api, messy):
    employees, logs = messy
    for e in employees:
        for month in ("2026-07", "2026-06", "2026-08"):
            got = api.get(f"/analytics/employees/{e['emp_code']}/monthly?month={month}").json()
            assert got == ref.monthly(employees, logs, e["emp_code"], month), (e["emp_code"], month)


def test_department_summary_matches_reference(api, messy):
    employees, logs = messy
    for month in ("2026-07", "2026-06", "2026-08"):
        assert api.get(f"/analytics/departments/summary?month={month}").json() == \
            ref.department_summary(employees, logs, month)
    assert api.get("/analytics/departments/summary?month=2026-07&department=Sales").json() == \
        ref.department_summary(employees, logs, "2026-07", "Sales")


def test_leaderboard_matches_reference(api, messy):
    employees, logs = messy
    for limit in (1, 3, 10, 50):
        for dept in (None, "Engineering", "Support"):
            q = f"month=2026-07&limit={limit}" + (f"&department={dept}" if dept else "")
            assert api.get("/analytics/leaderboard/late?" + q).json() == \
                ref.leaderboard(employees, logs, "2026-07", limit, dept), q


def test_trend_matches_reference(api, messy):
    employees, logs = messy
    for dept in ("Engineering", "Sales", "Support", "Ops"):
        got = api.get(f"/analytics/departments/{dept}/trend?from=2026-07-01&to=2026-08-05").json()
        assert got == ref.trend(employees, logs, dept, "2026-07-01", "2026-08-05"), dept


# --------------------------------------------------------------------------- #
# explain endpoint
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "LEAVE", "page": 2, "page_size": 10}),
    ("attendance_list", {"date_from": "2026-07-01"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Alpha"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Alpha", "limit": 5}),
    ("department_trend", {"department": "Alpha", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes_only(api, app_module, endpoint, params):
    seed(app_module, *awkward_dataset())
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    text = json.dumps(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text, text[:2000]


def test_explain_validation(api):
    assert api.get("/admin/explain/unknown").status_code == 422
    assert api.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422       # month missing
    assert api.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422          # emp_code missing
    assert api.get("/admin/explain/department_trend?department=A&from=2026-07-01").status_code == 422
    assert api.get("/admin/explain/department_summary").status_code == 422

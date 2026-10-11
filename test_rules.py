"""Unit tests for the business-rule helpers. They need no database."""
from datetime import datetime, timedelta, timezone

import pytest

from app import main as m

IST = m.IST


def ist(y, mo, d, h, mi, s=0):
    """An IST wall-clock time as an aware datetime."""
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# ---- R1: attendance date, whole-second truncation ------------------------------------------------
def test_ms_is_truncated_to_whole_seconds():
    assert m.dt_to_ms(m.ms_to_dt(1_783_312_500_900)) == 1_783_312_500_000


def test_date_is_the_ist_date_not_the_utc_date():
    # 00:30 IST on 7 July is still 6 July in UTC
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"


def test_overnight_early_morning_punch_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 7, 2, 0), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"  # not earlier than shift_end
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


# ---- R2: late ------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),     # exactly 10:00 after start -> not late
    (9, 40, 1, 10),    # 1 second over -> late, minutes counted from shift start
    (10, 15, 59, 45),
    (9, 0, 0, 0),      # early
    (9, 30, 0, 0),
])
def test_late_minutes(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30") == expected


def test_late_minutes_overnight_shift():
    # punch-in 22:11 for a 22:00 shift -> 11 minutes late
    assert m.compute_late_minutes(ist(2026, 7, 6, 22, 11), "2026-07-06", "22:00", "06:00") == 11
    # 02:00 next morning belongs to the shift that started at 22:00 the day before
    assert m.compute_late_minutes(ist(2026, 7, 7, 2, 0), "2026-07-06", "22:00", "06:00") == 240


# ---- R3: overtime --------------------------------------------------------------------------------
@pytest.mark.parametrize("h,mi,s,expected", [
    (18, 59, 59, 0),    # 29 min 59 s -> below the 30 minute minimum
    (19, 0, 0, 30),
    (19, 10, 59, 40),
    (18, 0, 0, 0),
])
def test_overtime(h, mi, s, expected):
    assert m.compute_overtime(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    # shift 22:00-06:00 started on the 6th; punch-out 06:40 on the 7th -> 40 minutes
    assert m.compute_overtime(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


# ---- R4 / R5: work hours, half day ---------------------------------------------------------------
def test_work_hours_round_half_up_not_bankers():
    t0 = ist(2026, 7, 6, 9, 0)
    # 4.505 h = 16218 s: float round() gives 4.5, half-up gives 4.51
    assert m.derive("PRESENT", t0, t0 + timedelta(seconds=16218), "2026-07-06", "09:30", "18:30")["work_hours"] == 4.51
    # 18 s = 0.005 h -> 0.01
    assert m.work_hours_hundredths(t0, t0 + timedelta(seconds=18)) == 1


def test_half_day_uses_the_rounded_value():
    t0 = ist(2026, 7, 6, 9, 0)
    # 4.495 h = 16182 s rounds up to 4.50 -> NOT a half day
    d = m.derive("PRESENT", t0, t0 + timedelta(seconds=16182), "2026-07-06", "09:30", "18:30")
    assert d["work_hours"] == 4.5 and d["half_day"] is False
    # 4.494 h -> 4.49 -> half day
    d = m.derive("PRESENT", t0, t0 + timedelta(seconds=16178), "2026-07-06", "09:30", "18:30")
    assert d["work_hours"] == 4.49 and d["half_day"] is True


def test_derive_defaults_for_open_and_absent_records():
    t0 = ist(2026, 7, 6, 10, 5)
    assert m.derive("PRESENT", t0, None, "2026-07-06", "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    assert m.derive("ABSENT", None, None, "2026-07-06", "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_sample_documents_from_the_data_model():
    # sample 2: 10:05 in (35 late), 19:10 out (40 overtime), 9.08 h
    d = m.derive("PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10), "2026-07-06", "09:30", "18:30")
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    # sample 7: overnight, 21:55 in, 06:40 out next day
    d = m.derive("PRESENT", ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00")
    assert d == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}
    # sample 4: 3.5 h -> half day
    d = m.derive("WFH", ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0), "2026-07-07", "09:30", "18:30")
    assert d["work_hours"] == 3.5 and d["half_day"] is True


# ---- R7 / R8 -------------------------------------------------------------------------------------
def test_working_days():
    assert m.working_days("2026-07-01", "2026-07-31", "2026-01-05") == 23
    assert m.working_days("2026-07-01", "2026-07-31", "2026-07-13") == 15   # mid-month joiner
    assert m.working_days("2026-07-01", "2026-07-31", "2026-08-03") == 0    # not joined yet


def test_half_up():
    assert m.half_up(2.675, 2) == 2.68
    assert m.half_up(0.00005, 4) == 0.0001
    assert m.half_up(0.5, 0) == 1.0


def test_month_range():
    assert m.month_range("2026-02") == ("2026-02-01", "2026-02-28")
    assert m.month_range("2028-02")[1] == "2028-02-29"


# ---- serialization -------------------------------------------------------------------------------
def test_record_out_defaults_and_ms_conversion_inside_history():
    when = datetime(2026, 7, 7, 3, 58, tzinfo=timezone.utc)
    legacy = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT", "punch_in": when,
              "punch_out": None, "work_hours": None, "late_minutes": 0}
    out = m.record_out(legacy)
    assert out["half_day"] is False and out["history"] == [] and out["overtime_minutes"] == 0
    assert "_id" not in out and "id" not in out
    corrected = {**legacy, "history": [{"at": when, "by": "hr", "reason": "x",
                                        "changes": {"punch_in": {"from": when, "to": None}}}]}
    h = m.record_out(corrected)["history"][0]
    assert h["at"] == 1_783_396_680_000 and h["changes"]["punch_in"] == {"from": 1_783_396_680_000, "to": None}


# ---- the integer half-up formulas used inside the aggregation pipelines ----------------------------
def test_pipeline_integer_rounding_formulas_match_decimal():
    import random
    from decimal import Decimal, ROUND_HALF_UP
    rng = random.Random(1)
    for _ in range(20000):
        headcount = rng.randint(1, 60)
        p2 = rng.randint(0, 2 * headcount * 2)               # 2 * present_count, multiples of 0.5
        rate_e4 = (10000 * p2 + headcount) // (2 * headcount)  # same formula as the $trend pipeline
        exact = Decimal(p2) / 2 / headcount
        assert rate_e4 == int((exact * 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        n = rng.randint(1, 7)
        w_sum = sum(rng.randint(0, 12000) for _ in range(n))
        moving = (2 * w_sum + n) // (2 * n)                   # moving_avg_7d formula
        assert moving == int((Decimal(w_sum) / n).quantize(Decimal(1), rounding=ROUND_HALF_UP))

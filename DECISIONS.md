# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for listing; `(department, joined_on)` and `(joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly and punch-in lookups); `(date -1, emp_code)` for the sorted list and the leaderboard; `(status, date -1, emp_code)` for status filters; `(emp_code, punch_in -1)` for punch-out. Rejected: an index on `department` inside logs, because logs do not store it.
2. **Punch-in race.** Both requests try `insert_one`. The unique `(emp_code, date)` index lets one succeed (201); the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.
3. **Ties.** The leaderboard uses `$rank` (1, 2, 2, 4) and applies `limit` afterwards, so a tie at the cutoff returns every tied employee, ordered by `emp_code`.
4. **Headcount.** The department summary starts from `employees` (joined on or before month end) and `$lookup`s logs, so employees with zero logs still count.
5. **100x data.** I would pre-aggregate monthly totals per employee on write instead of scanning daily logs each time.

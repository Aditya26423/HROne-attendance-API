# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual regularization with an audit trail, and analytics built as MongoDB aggregation pipelines. All application code is in `app/main.py`.

**Stack:** Python 3.11+, FastAPI, PyMongo, Pydantic, MongoDB 6.0+ (uses `$setWindowFields`, `$densify`).

## Run (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # then edit MONGO_URI / MONGO_DB if needed
uvicorn app.main:app --port 8000
```

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from the environment; real environment variables win over `.env`.
- Indexes are created automatically at startup (and again from `/health` if MongoDB was not up yet). No manual steps.
- Interactive docs: http://localhost:8000/docs  -  readiness: `GET /health`.
- MongoDB locally: install MongoDB Community 7.0, or use a free Atlas M0 cluster and put its connection string in `MONGO_URI`.
- Sample data (from the candidate kit): `python sample_seed.py` from the kit folder, with the same `MONGO_URI` / `MONGO_DB`.

## Tests

```powershell
pip install -r requirements-dev.txt
pytest tests/test_rules.py -q                    # business rules, no database needed
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
pytest -q                                        # full suite; needs MongoDB 6.0+
```

The API tests create a throw-away database `attendance_test_<random>` and drop it afterwards; they are skipped if MongoDB is unreachable. Analytics are compared against an independent pure-Python reference model in `tests/reference.py`.

## What is implemented

All endpoints in `openapi.yaml`: health, employees (create/list), punch-in, punch-out, attendance list, PATCH regularization with append-only `history`, the four analytics endpoints, and `/admin/explain/{endpoint}`. Rules R1-R10 are implemented as written in the contract.

## Notes for the reviewer

- Assumption: `moving_avg_7d` averages the *rounded* (4-decimal) `attendance_rate` values shown in the response.
- Assumption: a punch-out whose `punched_at` is earlier than every punch-in of the employee returns 404 ("no punch-in found"); equal to the punch-in returns 422.
- Queries use `hint` so plans are deterministic and explain output never contains a COLLSCAN or a rejected COLLSCAN plan.
- Concurrency relies on MongoDB: unique indexes plus atomic `find_one_and_update` with a state guard. Transactions are not used and are not needed (every write touches one document).
- Known limitation: I could not run the three aggregation pipelines against a real MongoDB in the environment where this was written; see the test instructions above.

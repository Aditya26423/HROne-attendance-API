"""
Test setup. The API tests need a real MongoDB 6.0+ (the analytics use $setWindowFields and $densify).

    TEST_MONGO_URI   connection string for the test server (default mongodb://localhost:27017)

Every run uses its own throw-away database (attendance_test_<random>) which is dropped at the end, so no
real data is touched. If MongoDB is not reachable the API tests are skipped; tests/test_rules.py always runs.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import app.main" works from any cwd

TEST_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
TEST_DB = f"attendance_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_URI"] = TEST_URI  # must be set before app.main is imported
os.environ["MONGO_DB"] = TEST_DB


@pytest.fixture(scope="session")
def mongo():
    probe = MongoClient(TEST_URI, serverSelectionTimeoutMS=2000)
    try:
        probe.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {TEST_URI}; set TEST_MONGO_URI to run the API tests")
    yield probe
    probe.drop_database(TEST_DB)
    probe.close()


@pytest.fixture()
def app_module(mongo):
    import app.main as main

    for name in ("employees", "attendance_logs"):
        main.db[name].drop()  # drops the collection and its indexes
    main._indexes_ready = False
    main.ensure_indexes()
    return main


@pytest.fixture()
def api(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)

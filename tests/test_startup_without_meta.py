from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
META_ENVIRONMENT_VARIABLES = (
    "META_ACCESS_TOKEN",
    "META_PHONE_NUMBER_ID",
    "META_GRAPH_VERSION",
    "META_WABA_ID",
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
)


def test_production_startup_health_and_readiness_without_meta() -> None:
    environment = os.environ.copy()
    for variable_name in META_ENVIRONMENT_VARIABLES:
        environment.pop(variable_name, None)
    environment.update(
        {
            "DATABASE_URL": (
                "postgresql+asyncpg://runtime@localhost:5432/runtime"
            ),
            "ENVIRONMENT": "production",
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (str(PROJECT_ROOT), environment.get("PYTHONPATH")),
                )
            ),
        }
    )
    startup_script = """
from fastapi.testclient import TestClient

from app.core.database import check_database_connection
from app.main import app

async def database_connected():
    return True

app.dependency_overrides[check_database_connection] = database_connected
with TestClient(app) as client:
    health = client.get('/health')
    ready = client.get('/ready')
assert health.status_code == 200
assert health.json() == {'status': 'ok'}
assert ready.status_code == 200
assert ready.json() == {'status': 'ready', 'database': 'connected'}
"""

    result = subprocess.run(
        [sys.executable, "-c", startup_script],
        cwd=PROJECT_ROOT / "tests",
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr

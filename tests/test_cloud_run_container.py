from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_has_cloud_run_runtime_contract() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    normalized = " ".join(dockerfile.split())

    assert dockerfile.startswith("FROM python:3.12-slim")
    assert "ENVIRONMENT=production" in dockerfile
    assert "PORT=8080" in dockerfile
    assert "USER app" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert r'--port \"$PORT\"' in dockerfile
    assert "--workers 1" in dockerfile
    assert "--timeout-graceful-shutdown 30" in dockerfile
    assert "--no-access-log" in dockerfile
    assert "--reload" not in dockerfile
    assert "alembic upgrade" not in normalized.casefold()
    assert "COPY --chown=app:app data ./data" in dockerfile


def test_dockerignore_excludes_local_secrets_and_build_artifacts() -> None:
    entries = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    for required_entry in (
        ".git",
        ".env",
        ".env.*",
        ".venv",
        "*.pem",
        "*.key",
        "tests",
        "build",
        "dist",
    ):
        assert required_entry in entries

from app.core.config import get_settings
from app.main import create_app


def route_paths(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_production_hides_fastapi_documentation(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        paths = route_paths(create_app())
        assert "/docs" not in paths
        assert "/redoc" not in paths
        assert "/openapi.json" not in paths
    finally:
        get_settings.cache_clear()


def test_non_production_keeps_documentation_for_local_development(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    try:
        paths = route_paths(create_app())
        assert "/docs" in paths
        assert "/redoc" in paths
        assert "/openapi.json" in paths
    finally:
        get_settings.cache_clear()

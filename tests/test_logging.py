import json
import logging

from app.core.logging import JsonFormatter, configure_logging


def test_json_formatter_ignores_sensitive_extra_fields() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "safe-request-id"
    record.token = "must-not-be-logged"
    record.phone = "must-not-be-logged"
    record.body = "must-not-be-logged"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "request_completed"
    assert payload["request_id"] == "safe-request-id"
    assert "token" not in payload
    assert "phone" not in payload
    assert "body" not in payload
    assert "must-not-be-logged" not in payload.values()


def test_uvicorn_logs_are_routed_through_root_json_handler() -> None:
    root_logger = logging.getLogger()
    uvicorn_logger = logging.getLogger("uvicorn")
    error_logger = logging.getLogger("uvicorn.error")
    access_logger = logging.getLogger("uvicorn.access")
    original = {
        "root_handlers": root_logger.handlers[:],
        "root_level": root_logger.level,
        "uvicorn_handlers": uvicorn_logger.handlers[:],
        "uvicorn_propagate": uvicorn_logger.propagate,
        "error_handlers": error_logger.handlers[:],
        "error_propagate": error_logger.propagate,
        "access_handlers": access_logger.handlers[:],
        "access_disabled": access_logger.disabled,
    }
    try:
        configure_logging("production")

        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0].formatter, JsonFormatter)
        assert uvicorn_logger.handlers == []
        assert uvicorn_logger.propagate is True
        assert error_logger.handlers == []
        assert error_logger.propagate is True
        assert access_logger.handlers == []
        assert access_logger.disabled is True
    finally:
        root_logger.handlers = original["root_handlers"]
        root_logger.setLevel(original["root_level"])
        uvicorn_logger.handlers = original["uvicorn_handlers"]
        uvicorn_logger.propagate = original["uvicorn_propagate"]
        error_logger.handlers = original["error_handlers"]
        error_logger.propagate = original["error_propagate"]
        access_logger.handlers = original["access_handlers"]
        access_logger.disabled = original["access_disabled"]

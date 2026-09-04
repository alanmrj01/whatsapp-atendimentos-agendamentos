from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.public_pwa import router as public_pwa_router
from app.api.platform_admin import router as platform_admin_router

from app.api.health import router as health_router
from app.api.diagnostics import router as diagnostics_router
from app.api.internal_tasks import router as internal_tasks_router
from app.api.internal_whatsapp_onboarding import (
    router as internal_whatsapp_onboarding_router,
)
from app.api.whatsapp_webhook import router as whatsapp_webhook_router
from app.core.config import Environment, get_settings
from app.core.database import dispose_engine
from app.core.logging import configure_logging
from app.tasks.cloud_tasks import close_cloud_tasks_client

settings = get_settings()
configure_logging(settings.environment.value)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.initialized = True
    try:
        yield
    finally:
        application.state.initialized = False
        try:
            await close_cloud_tasks_client()
        finally:
            await dispose_engine()


def create_app() -> FastAPI:
    current_settings = get_settings()
    production = current_settings.environment is Environment.production
    application = FastAPI(
        title="WhatsApp Atendimento e Agendamento",
        version="0.1.0",
        debug=False,
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )

    application.include_router(health_router)
    application.include_router(public_pwa_router)
    application.include_router(platform_admin_router)
    origins = current_settings.allowed_pwa_origins()
    application.add_middleware(
        CORSMiddleware, allow_origins=list(origins), allow_credentials=bool(origins),
        allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type"],
    )
    application.state.initialized = False
    application.include_router(diagnostics_router)
    application.include_router(internal_tasks_router)
    application.include_router(internal_whatsapp_onboarding_router)
    application.include_router(whatsapp_webhook_router)

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, _: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        logger.error(
            "unhandled_exception",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )

    @application.middleware("http")
    async def structured_request_logging(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = uuid4().hex
        request.state.request_id = request_id
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.error(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                },
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id, **(
                    {"Cache-Control": "no-store", "Pragma": "no-cache"}
                    if request.url.path.startswith("/api/") else {}
                )},
            )

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    return application


app = create_app()

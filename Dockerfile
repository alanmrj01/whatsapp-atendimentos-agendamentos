FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    ENVIRONMENT=production

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements-prod.txt .
RUN pip install --disable-pip-version-check -r requirements-prod.txt

COPY --chown=app:app app ./app
COPY --chown=app:app data ./data
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./alembic.ini

USER app

EXPOSE 8080

STOPSIGNAL SIGTERM

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\" --workers 1 --timeout-graceful-shutdown 30 --no-access-log"]

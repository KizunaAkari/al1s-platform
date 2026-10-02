ARG PYTHON_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
FROM ${PYTHON_IMAGE} AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock

FROM dependencies AS source
COPY al1s ./al1s
COPY alembic.ini ./
COPY migrations ./migrations

FROM dependencies AS test-dependencies
COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir -r requirements-dev.lock

FROM test-dependencies AS test
COPY al1s ./al1s
COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests
COPY contracts ./contracts
CMD ["python", "-m", "pytest"]

FROM source AS runtime
RUN useradd --create-home --uid 10001 al1s
USER al1s
EXPOSE 8000
CMD ["uvicorn", "al1s.main:app", "--host", "0.0.0.0", "--port", "8000"]

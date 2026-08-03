ARG BASE_IMAGE_REGISTRY=m.daocloud.io/docker.io/library

FROM ${BASE_IMAGE_REGISTRY}/node:24-alpine AS frontend-build

WORKDIR /build/frontend
COPY frontend/package*.json ./
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm ci --registry=${NPM_REGISTRY} --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${BASE_IMAGE_REGISTRY}/python:3.12-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTEND_DIST=/app/frontend/dist \
    DATABASE_PATH=/app/data/control-center.db

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY VERSION /app/VERSION
COPY server/ /app/server/
COPY --from=frontend-build /build/frontend/dist/ /app/frontend/dist/

RUN mkdir -p /app/data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

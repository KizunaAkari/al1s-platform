ARG NODE_IMAGE=m.daocloud.io/docker.io/library/node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd
ARG PYTHON_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /frontend
COPY al1s-frontend/package.json al1s-frontend/package-lock.json ./
RUN npm ci
COPY al1s-frontend/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 AL1S_FRONTEND_DIRECTORY=/app/frontend
WORKDIR /app
COPY backend/al1s-backend/requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock supervisor==4.3.0
COPY backend/al1s-backend/al1s ./al1s
COPY backend/al1s-backend/alembic.ini ./
COPY backend/al1s-backend/migrations ./migrations
COPY --from=frontend-build /frontend/dist ./frontend
COPY al1s-deployment/platform/supervisord.conf /etc/al1s/supervisord.conf
COPY al1s-deployment/platform/healthcheck.py /app/healthcheck.py
RUN useradd --create-home --uid 10001 al1s && mkdir /run/al1s && chown al1s:al1s /run/al1s
USER al1s
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=10s --start-period=30s --retries=3 CMD ["python", "/app/healthcheck.py"]
CMD ["supervisord", "-c", "/etc/al1s/supervisord.conf"]

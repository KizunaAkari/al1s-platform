ARG MAINTENANCE_IMAGE
ARG DOCKER_CLI_IMAGE=m.daocloud.io/docker.io/library/docker:29.8.2-cli@sha256:35cfcf893c20f2931f4909151b5407aaaeab711277de7ce8b8f521f095d54c32
FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${MAINTENANCE_IMAGE}
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY backend/al1s-backend/al1s /app/al1s
COPY al1s-deployment/ops/supervisord.conf /etc/al1s/ops-supervisord.conf
COPY al1s-deployment/ops/entry.py /app/ops-entry.py
COPY al1s-deployment/ops/healthcheck.py /app/ops-healthcheck.py
HEALTHCHECK --interval=15s --timeout=10s --start-period=25s --retries=3 CMD ["python", "/app/ops-healthcheck.py"]
CMD ["python", "/app/ops-entry.py"]
RUN python -c "from al1s.components.host.app import create_host; from al1s.bots.control_proxy import from_environment"
RUN docker buildx version

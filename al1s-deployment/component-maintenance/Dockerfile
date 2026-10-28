ARG DOCKER_CLI_IMAGE=m.daocloud.io/docker.io/library/docker:29.8.2-cli@sha256:35cfcf893c20f2931f4909151b5407aaaeab711277de7ce8b8f521f095d54c32
ARG PLATFORM_IMAGE
FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${PLATFORM_IMAGE}
USER root
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY backend/al1s-backend/al1s/components/host /app/al1s/components/host
COPY al1s-deployment/component-maintenance/healthcheck.py /app/component-healthcheck.py
USER al1s
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD ["python", "/app/component-healthcheck.py"]
CMD ["python", "-m", "al1s.components.host"]

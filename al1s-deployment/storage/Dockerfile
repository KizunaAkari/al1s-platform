ARG SEAWEED_IMAGE
ARG PLATFORM_IMAGE
FROM ${SEAWEED_IMAGE} AS seaweed
FROM ${PLATFORM_IMAGE}
USER root
COPY --from=seaweed /usr/bin/weed /usr/bin/weed
COPY al1s-deployment/storage/supervisord.conf /etc/al1s/storage-supervisord.conf
COPY al1s-deployment/storage/entry.py /app/storage-entry.py
COPY al1s-deployment/storage/healthcheck.py /app/storage-healthcheck.py
COPY al1s-deployment/storage/layout_probe.py /app/storage-layout-probe.py
RUN mkdir -p /data/master /data/volume /data/filer /data/s3 /run/al1s-storage
LABEL org.al1s.storage-layout="seaweed-four-process-v1"
HEALTHCHECK --interval=10s --timeout=8s --start-period=40s --retries=3 CMD ["python", "/app/storage-healthcheck.py"]
ENTRYPOINT ["python", "/app/storage-entry.py"]
CMD []

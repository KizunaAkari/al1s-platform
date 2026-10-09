ARG MAINTENANCE_IMAGE
FROM ${MAINTENANCE_IMAGE}
COPY backend/al1s-backend/al1s/components/host /app/al1s/components/host
COPY backend/al1s-backend/al1s/components/contracts.py backend/al1s-backend/al1s/components/types.py backend/al1s-backend/al1s/components/host_client.py backend/al1s-backend/al1s/components/inventory.py backend/al1s-backend/al1s/components/operations.py /app/al1s/components/
COPY backend/al1s-backend/al1s/bots/control_proxy.py /app/al1s/bots/control_proxy.py
COPY al1s-deployment/ops/supervisord.conf /etc/al1s/ops-supervisord.conf
COPY al1s-deployment/ops/entry.py /app/ops-entry.py
COPY al1s-deployment/ops/healthcheck.py /app/ops-healthcheck.py
HEALTHCHECK --interval=15s --timeout=10s --start-period=25s --retries=3 CMD ["python", "/app/ops-healthcheck.py"]
CMD ["python", "/app/ops-entry.py"]

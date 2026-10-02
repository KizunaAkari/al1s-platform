from __future__ import annotations

import socket
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.config import Config
from sqlalchemy import Engine

from al1s.app.config import Settings
from al1s.infrastructure.database import DatabaseProbe, create_database_engine


class ClosableProbe(Protocol):
    def check(self) -> None: ...

    def close(self) -> None: ...


class S3Probe:
    def __init__(self, settings: Settings) -> None:
        timeout = settings.probe_timeout_seconds
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            config=Config(
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"max_attempts": 0},
                s3={"addressing_style": "path"},
            ),
        )

    def check(self) -> None:
        self._client.head_bucket(Bucket=self._bucket)

    def close(self) -> None:
        self._client.close()


class MqttProbe:
    def __init__(self, settings: Settings) -> None:
        self._host = settings.mqtt_host
        self._port = settings.mqtt_port
        self._timeout = settings.probe_timeout_seconds

    def check(self) -> None:
        with socket.create_connection((self._host, self._port), timeout=self._timeout):
            return

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    dependencies: dict[str, dict[str, str]]

    def as_dict(self, request_id: str) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "dependencies": self.dependencies,
            "request_id": request_id,
        }


class ReadinessService:
    def __init__(self, probes: Mapping[str, ClosableProbe]) -> None:
        self._probes = dict(probes)

    def check(self) -> ReadinessResult:
        results: dict[str, dict[str, str]] = {}
        with ThreadPoolExecutor(max_workers=len(self._probes)) as executor:
            futures = {executor.submit(probe.check): name for name, probe in self._probes.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    results[name] = {"status": "ready"}
                except Exception as exc:  # dependency adapters define concrete errors
                    results[name] = {"status": "not_ready", "reason": type(exc).__name__}
        ordered = {name: results[name] for name in self._probes}
        return ReadinessResult(
            ready=all(item["status"] == "ready" for item in ordered.values()),
            dependencies=ordered,
        )

    def close(self) -> None:
        for probe in self._probes.values():
            probe.close()


def build_readiness_service(
    settings: Settings,
    database_engine: Engine | None = None,
) -> ReadinessService:
    resolved_engine = database_engine or create_database_engine(settings)
    probes: dict[str, ClosableProbe] = {
        "postgresql": DatabaseProbe(resolved_engine, owns_engine=database_engine is None),
        "s3": S3Probe(settings),
        "mqtt": MqttProbe(settings),
    }
    return ReadinessService(probes)

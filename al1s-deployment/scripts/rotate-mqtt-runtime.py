"""One-time MQTT credential rotation for the r47 production cutover.

The default mode checks the current broker and session state without mutation.
Apply only after all platform Supervisor processes have stopped.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import psycopg
from al1s.adapters.mqtt.mosquitto import MosquittoDynamicSecurityBroker
from paho.mqtt.client import CallbackAPIVersion, Client, MQTTv5


def candidate_value(path: Path, name: str) -> str:
    values = [
        line.partition("=")[2]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(name + "=")
    ]
    if len(values) != 1 or len(values[0].encode("utf-8")) < 16:
        raise ValueError(f"candidate {name} is missing or too short")
    return values[0]


def require_quiesced() -> None:
    result = subprocess.run(
        ["supervisorctl", "-c", "/etc/al1s/supervisord.conf", "status"],
        capture_output=True, text=True, check=False,
    )
    expected = {
        "api", "events", "media", "notifications", "release-verifier",
        "scheduler", "terminal-monitor",
    }
    states = {
        parts[0]: parts[1]
        for line in result.stdout.splitlines()
        if len(parts := line.split()) >= 2
    }
    if result.returncode not in {0, 1, 3} or set(states) != expected or any(
        state != "STOPPED" for state in states.values()
    ):
        raise RuntimeError("stop all platform application processes before applying")


def verify_login(host: str, port: int, username: str, password: str,
                 client_id: str = "") -> None:
    completed = threading.Event()
    result: list[bool] = []
    client = Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=client_id, protocol=MQTTv5,
    )
    client.username_pw_set(username, password)

    def on_connect(_client: Client, _user: object, _flags: object,
                   reason: object, _properties: object) -> None:
        result.append(not reason.is_failure)
        completed.set()

    client.on_connect = on_connect
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        if not completed.wait(5) or result != [True]:
            raise RuntimeError("rotated MQTT credential failed broker login")
    finally:
        client.disconnect()
        client.loop_stop()


def revoke_live_sessions(conn: psycopg.Connection[object],
                         broker: MosquittoDynamicSecurityBroker) -> int:
    rows = conn.execute(
        "SELECT id, username, role_name, row_version FROM terminal_mqtt_sessions "
        "WHERE status IN ('active', 'pending') AND expires_at > now() FOR UPDATE"
    ).fetchall()
    # This maintenance is for the verified single live session, not a bulk revoker.
    if len(rows) > 1:
        raise RuntimeError("unexpected number of live MQTT sessions")
    for session_id, username, role_name, row_version in rows:
        broker.revoke_terminal_subscriber(SimpleNamespace(
            username=username, role_name=role_name,
        ))
        updated = conn.execute(
            "UPDATE terminal_mqtt_sessions SET status = 'revoked', "
            "revoked_at = now(), row_version = row_version + 1 "
            "WHERE id = %s AND row_version = %s AND status IN ('active', 'pending') "
            "RETURNING id", (session_id, row_version),
        ).fetchone()
        if updated is None:
            raise RuntimeError("MQTT session changed during revocation")
    conn.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    host = os.environ["AL1S_MQTT_HOST"]
    port = int(os.environ["AL1S_MQTT_PORT"])
    admin = os.environ["AL1S_MQTT_USERNAME"]
    publisher = os.environ["AL1S_MQTT_PUBLISHER_USERNAME"]
    old_admin = os.environ["AL1S_MQTT_PASSWORD"]
    new_admin = candidate_value(args.candidate, "AL1S_MQTT_PASSWORD")
    new_publisher = candidate_value(args.candidate, "AL1S_MQTT_PUBLISHER_PASSWORD")
    if old_admin == new_admin or os.environ["AL1S_MQTT_PUBLISHER_PASSWORD"] == new_publisher:
        raise ValueError("candidate MQTT credentials match current credentials")
    broker = MosquittoDynamicSecurityBroker(
        host=host, port=port, admin_username=admin, admin_password=old_admin,
    )
    for username in (admin, publisher):
        response = broker._round_trip(({"command": "getClient", "username": username},))
        if not response.get("responses") or response["responses"][0].get("error"):
            raise RuntimeError("expected MQTT client is unavailable")

    database_url = os.environ["AL1S_DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1,
    )
    with psycopg.connect(database_url) as conn:
        if args.apply:
            require_quiesced()
            revoked = revoke_live_sessions(conn, broker)
        else:
            revoked = conn.execute(
                "SELECT count(*) FROM terminal_mqtt_sessions "
                "WHERE status IN ('active', 'pending') AND expires_at > now()"
            ).fetchone()[0]
    if args.apply:
        broker._execute(({
            "command": "setClientPassword", "username": publisher,
            "password": new_publisher,
        },), allowed_errors=())
        verify_login(host, port, publisher, new_publisher, client_id=publisher)
        broker._execute(({
            "command": "setClientPassword", "username": admin,
            "password": new_admin,
        },), allowed_errors=())
        verify_login(host, port, admin, new_admin)
    print("mode=" + ("applied" if args.apply else "checked"))
    print("live_sessions=" + str(revoked))


if __name__ == "__main__":
    main()

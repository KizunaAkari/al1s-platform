"""One-time, transactional rotation of platform-encrypted database fields.

Run inside the current platform image. Without --apply, exercise every update
and verification in a transaction that is rolled back. The --apply path requires
all Supervisor application processes to be stopped before it commits.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from uuid import UUID

import psycopg
from al1s.secrets.security import FernetSecretCipher
from psycopg import sql

# All identifiers are fixed application schema, never supplied by the caller.
ENCRYPTED_FIELDS = (
    ("encrypted_secrets", "ciphertext", True),
    ("information_message_reviews", "body_cipher", False),
    ("interactive_sessions", "ciphertext", False),
)


def candidate_key(path: Path) -> str:
    values = [
        line.partition("=")[2]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("AL1S_NOTIFICATION_MASTER_KEY=")
    ]
    if len(values) != 1 or len(values[0].encode("utf-8")) < 32:
        raise ValueError("candidate master key is missing or too short")
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


def rotate_field(
    conn: psycopg.Connection[object], table: str, column: str, has_updated_at: bool,
    old: FernetSecretCipher, new: FernetSecretCipher,
) -> int:
    select_query = sql.SQL(
        "SELECT id, {}, key_id FROM {} WHERE {} IS NOT NULL FOR UPDATE"
    ).format(sql.Identifier(column), sql.Identifier(table), sql.Identifier(column))
    rows = conn.execute(select_query).fetchall()
    if not rows:
        return 0

    ids: list[UUID] = []
    ciphertexts: list[str] = []
    for row_id, ciphertext, key_id in rows:
        plaintext = old.decrypt(ciphertext, key_id)
        replacement = new.encrypt(plaintext)
        if new.decrypt(replacement, new.key_id) != plaintext:
            raise RuntimeError("rotated secret failed round-trip verification")
        ids.append(row_id)
        ciphertexts.append(replacement)

    updated_at = sql.SQL(", updated_at = now()") if has_updated_at else sql.SQL("")
    update_query = sql.SQL(
        "UPDATE {} AS t SET {} = v.ciphertext, key_id = %s, "
        "row_version = t.row_version + 1{} "
        "FROM unnest(%s::uuid[], %s::text[]) AS v(id, ciphertext) "
        "WHERE t.id = v.id AND t.key_id = %s RETURNING t.id"
    ).format(sql.Identifier(table), sql.Identifier(column), updated_at)
    updated = conn.execute(update_query, (new.key_id, ids, ciphertexts, old.key_id)).fetchall()
    if len(updated) != len(ids):
        raise RuntimeError("a protected row changed during rotation")

    check_query = sql.SQL(
        "SELECT {}, key_id FROM {} WHERE id = ANY(%s::uuid[])"
    ).format(sql.Identifier(column), sql.Identifier(table))
    verified = conn.execute(check_query, (ids,)).fetchall()
    if len(verified) != len(ids):
        raise RuntimeError("rotated row count changed during verification")
    for ciphertext, key_id in verified:
        new.decrypt(ciphertext, key_id)
    return len(ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()

    old_value = os.environ["AL1S_NOTIFICATION_MASTER_KEY"]
    new_value = candidate_key(arguments.candidate)
    if old_value == new_value:
        raise ValueError("candidate master key matches current key")
    if arguments.apply:
        require_quiesced()
    old, new = FernetSecretCipher(old_value), FernetSecretCipher(new_value)
    database_url = os.environ["AL1S_DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1,
    )
    with psycopg.connect(database_url) as conn:
        counts = [
            rotate_field(conn, table, column, has_updated_at, old, new)
            for table, column, has_updated_at in ENCRYPTED_FIELDS
        ]
        if not arguments.apply:
            conn.rollback()
    print("mode=" + ("applied" if arguments.apply else "rolled_back_check"))
    print("encrypted_rows=" + ",".join(map(str, counts)))


if __name__ == "__main__":
    main()

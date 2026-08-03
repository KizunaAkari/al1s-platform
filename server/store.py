import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = settings.database_path):
        self.path = path
        self._command_condition = threading.Condition()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                os TEXT NOT NULL,
                address TEXT,
                capabilities TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'offline',
                last_seen TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL,
                script_name TEXT NOT NULL DEFAULT '',
                script TEXT,
                params TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                result TEXT NOT NULL DEFAULT '{}',
                batch_id TEXT,
                batch_sequence INTEGER NOT NULL DEFAULT 1,
                schedule_type TEXT NOT NULL DEFAULT 'once',
                scheduled_for TEXT,
                schedule_window_end TEXT,
                run_index INTEGER NOT NULL DEFAULT 1,
                run_total INTEGER NOT NULL DEFAULT 1,
                root_task_id TEXT,
                retry_of_task_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                max_retries INTEGER NOT NULL DEFAULT 0,
                record_video INTEGER NOT NULL DEFAULT 0,
                recording_path TEXT,
                recording_mime TEXT,
                recording_size INTEGER,
                task_kind TEXT NOT NULL DEFAULT 'standard',
                composition TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                finished_at TEXT,
                result TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'INFO',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scripts (
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                category_package TEXT,
                source_package TEXT,
                source_activity TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(agent_id, name)
            );
            CREATE TABLE IF NOT EXISTS script_categories (
                agent_id TEXT NOT NULL,
                package_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(agent_id, package_name)
            );
            CREATE TABLE IF NOT EXISTS script_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                script_name TEXT NOT NULL DEFAULT '',
                operation TEXT NOT NULL,
                from_category TEXT,
                to_category TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failure_records (
                id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL,
                task_id TEXT,
                agent_id TEXT NOT NULL,
                script_name TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL,
                failed_step_index INTEGER,
                failed_step_action TEXT,
                screenshot_path TEXT,
                screenshot_mime TEXT,
                screenshot_size INTEGER,
                email_status TEXT NOT NULL DEFAULT 'disabled',
                email_error TEXT,
                result TEXT NOT NULL DEFAULT '{}',
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                smtp_host TEXT NOT NULL DEFAULT '',
                smtp_port INTEGER NOT NULL DEFAULT 587,
                smtp_user TEXT NOT NULL DEFAULT '',
                smtp_password_encrypted TEXT NOT NULL DEFAULT '',
                smtp_from TEXT NOT NULL DEFAULT '',
                smtp_starttls INTEGER NOT NULL DEFAULT 1,
                smtp_ssl INTEGER NOT NULL DEFAULT 0,
                failure_recipients TEXT NOT NULL DEFAULT '[]',
                failure_enabled INTEGER NOT NULL DEFAULT 0,
                public_base_url TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_alerts (
                scope TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                free_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL,
                last_sent TEXT,
                last_attempt TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_failure_records_created_at
                ON failure_records(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_failure_records_agent_id
                ON failure_records(agent_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_script_audit_agent
                ON script_audit_logs(agent_id, created_at DESC);
            """)
            task_columns = {
                "script_name": "TEXT NOT NULL DEFAULT ''",
                "batch_id": "TEXT",
                "batch_sequence": "INTEGER NOT NULL DEFAULT 1",
                "schedule_type": "TEXT NOT NULL DEFAULT 'once'",
                "scheduled_for": "TEXT",
                "schedule_window_end": "TEXT",
                "run_index": "INTEGER NOT NULL DEFAULT 1",
                "run_total": "INTEGER NOT NULL DEFAULT 1",
                "root_task_id": "TEXT",
                "retry_of_task_id": "TEXT",
                "attempt": "INTEGER NOT NULL DEFAULT 1",
                "max_retries": "INTEGER NOT NULL DEFAULT 0",
                "record_video": "INTEGER NOT NULL DEFAULT 0",
                "recording_path": "TEXT",
                "recording_mime": "TEXT",
                "recording_size": "INTEGER",
                "task_kind": "TEXT NOT NULL DEFAULT 'standard'",
                "composition": "TEXT NOT NULL DEFAULT '[]'",
            }
            existing = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            for name, declaration in task_columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            storage_columns = {row["name"] for row in db.execute("PRAGMA table_info(storage_alerts)")}
            if "last_attempt" not in storage_columns:
                db.execute("ALTER TABLE storage_alerts ADD COLUMN last_attempt TEXT")
            if "last_error" not in storage_columns:
                db.execute("ALTER TABLE storage_alerts ADD COLUMN last_error TEXT")
            failure_columns = {row["name"] for row in db.execute("PRAGMA table_info(failure_records)")}
            if "confirmed" not in failure_columns:
                db.execute("ALTER TABLE failure_records ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
            script_columns = {row["name"] for row in db.execute("PRAGMA table_info(scripts)")}
            for name in ("category_package", "source_package", "source_activity"):
                if name not in script_columns:
                    db.execute(f"ALTER TABLE scripts ADD COLUMN {name} TEXT")
            legacy_scripts = db.execute(
                """SELECT agent_id,name,content FROM scripts
                WHERE category_package IS NULL AND source_package IS NULL"""
            ).fetchall()
            for script in legacy_scripts:
                try:
                    document = json.loads(script["content"])
                    launch = next(
                        step
                        for step in document.get("steps", [])
                        if isinstance(step, dict) and step.get("action") == "launch_app"
                    )
                    raw_package = str(launch.get("package") or "").strip()
                    raw_activity = str(launch.get("activity") or "").strip()
                    if "/" in raw_package:
                        raw_package, embedded_activity = raw_package.split("/", 1)
                        raw_activity = raw_activity or embedded_activity
                    if not raw_package:
                        continue
                except (AttributeError, json.JSONDecodeError, StopIteration, TypeError):
                    continue
                now = utc_now()
                db.execute(
                    """INSERT INTO script_categories(
                        agent_id,package_name,display_name,created_at,updated_at
                    ) VALUES(?,?,?,?,?)
                    ON CONFLICT(agent_id,package_name) DO NOTHING""",
                    (script["agent_id"], raw_package, raw_package, now, now),
                )
                db.execute(
                    """UPDATE scripts
                    SET category_package=?,source_package=?,source_activity=?
                    WHERE agent_id=? AND name=?""",
                    (
                        raw_package,
                        raw_package,
                        raw_activity or None,
                        script["agent_id"],
                        script["name"],
                    ),
                )
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_schedule ON tasks(status, scheduled_for, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id, batch_sequence, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_scripts_category ON scripts(agent_id, category_package, name)")

    @staticmethod
    def decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("capabilities", "metadata", "params", "result", "payload", "composition"):
            if key in item:
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        for key in ("record_video", "confirmed"):
            if key in item:
                item[key] = bool(item[key])
        return item

    def upsert_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute("""INSERT INTO agents(id,name,os,address,capabilities,status,last_seen,metadata)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os,
                address=excluded.address, capabilities=excluded.capabilities,
                status='online', last_seen=excluded.last_seen, metadata=excluded.metadata""",
                (agent["id"], agent["name"], agent["os"], agent.get("address"),
                 json.dumps(agent.get("capabilities", {})), "online", now,
                 json.dumps(agent.get("metadata", {}))))
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent["id"],)).fetchone()
        return self.decode(row)

    def heartbeat(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("UPDATE agents SET status='online', last_seen=?, metadata=? WHERE id=?",
                       (now, json.dumps(payload), agent_id))
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return self.decode(row)

    def mark_stale(self, timeout_seconds: int):
        with self.connect() as db:
            db.execute("""UPDATE agents SET status='offline'
                WHERE last_seen IS NULL OR (julianday(?) - julianday(last_seen)) * 86400 > ?""",
                       (utc_now(), timeout_seconds))

    def list_agents(self):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM agents ORDER BY name")]

    def overview(self) -> dict[str, int]:
        with self.connect() as db:
            agent_row = db.execute("""SELECT COUNT(*) AS total,
                SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) AS online FROM agents""").fetchone()
            task_row = db.execute("""SELECT
                SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM tasks""").fetchone()
            failure_row = db.execute(
                "SELECT COUNT(*) AS total FROM failure_records WHERE confirmed=0"
            ).fetchone()
        return {
            "agents_total": int(agent_row["total"] or 0),
            "agents_online": int(agent_row["online"] or 0),
            "tasks_running": int(task_row["running"] or 0),
            "tasks_succeeded": int(task_row["succeeded"] or 0),
            "tasks_failed": int(task_row["failed"] or 0),
            "failure_records": int(failure_row["total"] or 0),
        }

    def get_agent(self, agent_id: str):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    @staticmethod
    def _task_values(task: dict[str, Any]) -> tuple[Any, ...]:
        task_id = task["id"]
        return (
            task_id,
            task["agent_id"],
            task["name"],
            task.get("script_name", ""),
            task.get("script"),
            json.dumps(task.get("params", {})),
            task.get("status", "queued"),
            json.dumps(task.get("result", {})),
            task.get("batch_id"),
            int(task.get("batch_sequence", 1)),
            task.get("schedule_type", "once"),
            task.get("scheduled_for"),
            task.get("schedule_window_end"),
            int(task.get("run_index", 1)),
            int(task.get("run_total", 1)),
            task.get("root_task_id") or task_id,
            task.get("retry_of_task_id"),
            int(task.get("attempt", 1)),
            int(task.get("max_retries", 0)),
            int(bool(task.get("record_video", False))),
            task.get("recording_path"),
            task.get("recording_mime"),
            task.get("recording_size"),
            task.get("task_kind", "standard"),
            json.dumps(task.get("composition", [])),
            task.get("created_at", utc_now()),
            task.get("started_at"),
            task.get("finished_at"),
        )

    @classmethod
    def _insert_task(cls, db: sqlite3.Connection, task: dict[str, Any]):
        db.execute("""INSERT INTO tasks(
            id,agent_id,name,script_name,script,params,status,result,batch_id,batch_sequence,
            schedule_type,scheduled_for,schedule_window_end,run_index,run_total,
            root_task_id,retry_of_task_id,attempt,max_retries,record_video,
            recording_path,recording_mime,recording_size,task_kind,composition,
            created_at,started_at,finished_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", cls._task_values(task))

    @staticmethod
    def _insert_command(db: sqlite3.Connection, command: dict[str, Any]):
        db.execute("""INSERT INTO commands(id,agent_id,kind,payload,status,created_at)
            VALUES(?,?,?,?,?,?)""", (
            command["id"], command["agent_id"], command["kind"],
            json.dumps(command.get("payload", {})), command.get("status", "pending"),
            command.get("created_at", utc_now()),
        ))

    @staticmethod
    def task_command_payload(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task["id"],
            "name": task["name"],
            "script_name": task.get("script_name", ""),
            "script": task.get("script"),
            "params": task.get("params", {}),
            "record_video": bool(task.get("record_video", False)),
            "attempt": int(task.get("attempt", 1)),
            "max_retries": int(task.get("max_retries", 0)),
            "task_kind": task.get("task_kind", "standard"),
            "composition": task.get("composition", []),
        }

    def create_task(self, task: dict[str, Any], command: dict[str, Any] | None = None):
        with self._command_condition:
            with self.connect() as db:
                self._insert_task(db, task)
                if command:
                    self._insert_command(db, command)
            self._command_condition.notify_all()
        return self.get_task(task["id"])

    def create_task_batch(
        self,
        tasks: list[dict[str, Any]],
        commands: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not tasks:
            return []
        with self._command_condition:
            with self.connect() as db:
                for task in tasks:
                    self._insert_task(db, task)
                for command in commands:
                    self._insert_command(db, command)
            self._command_condition.notify_all()
        task_ids = [task["id"] for task in tasks]
        with self.connect() as db:
            placeholders = ",".join("?" for _ in task_ids)
            rows = db.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY created_at",
                task_ids,
            ).fetchall()
        return [self.decode(row) for row in rows]

    def create_command(self, command: dict[str, Any]):
        with self._command_condition:
            with self.connect() as db:
                db.execute("""INSERT INTO commands(id,agent_id,kind,payload,status,created_at)
                    VALUES(?,?,?,?,?,?)""", (command["id"], command["agent_id"], command["kind"],
                    json.dumps(command.get("payload", {})), "pending", utc_now()))
            self._command_condition.notify_all()
        return self.get_command(command["id"])

    def list_tasks(self, agent_id: str | None = None, limit: int = 1000):
        with self.connect() as db:
            query, values = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            if agent_id:
                query = "SELECT * FROM tasks WHERE agent_id=? ORDER BY created_at DESC LIMIT ?"
                values = (agent_id, limit)
            return [self.decode(row) for row in db.execute(query, values)]

    def get_task(self, task_id: str):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def activate_due_tasks(self, now: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        due_at = now or utc_now()
        activated: list[str] = []
        with self._command_condition:
            with self.connect() as db:
                rows = db.execute("""SELECT * FROM tasks
                    WHERE status='scheduled' AND scheduled_for IS NOT NULL AND scheduled_for<=?
                    ORDER BY scheduled_for,batch_sequence,created_at LIMIT ?""", (due_at, limit)).fetchall()
                for index, row in enumerate(rows):
                    task = self.decode(row)
                    command = {
                        "id": str(uuid.uuid4()),
                        "agent_id": task["agent_id"],
                        "kind": "task",
                        "payload": self.task_command_payload(task),
                        "created_at": utc_now(),
                    }
                    updated = db.execute(
                        "UPDATE tasks SET status='queued' WHERE id=? AND status='scheduled'",
                        (task["id"],),
                    )
                    if updated.rowcount:
                        self._insert_command(db, command)
                        activated.append(task["id"])
            if activated:
                self._command_condition.notify_all()
        return [self.get_task(task_id) for task_id in activated]

    @staticmethod
    def _composition_retry_payload(
        task: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Build a retry that replays Start then resumes the failed module.

        Module indexes embedded in the compiled DSL stay tied to the original
        composition. Popup step indexes are the exception: MaaFramework sees
        the trimmed retry script, so those indexes must be remapped after
        completed modules are removed.
        """
        if task.get("task_kind") != "composition":
            return None
        try:
            document = json.loads(str(task.get("script") or ""))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or document.get("script_type") != "composition":
            return None
        steps = document.get("steps")
        if not isinstance(steps, list):
            return None

        result = task.get("result")
        retry_state = result.get("composition_retry") if isinstance(result, dict) else None
        if not isinstance(retry_state, dict):
            # An older Agent that did not persist composition failure metadata
            # cannot provide a task-level checkpoint and must retry in full.
            return None
        failed_step = result.get("failed_step") if isinstance(result, dict) else None
        if not isinstance(failed_step, dict):
            return None

        resume_module_index: int | None = None
        interval_failure = False
        failed_module = failed_step.get("module")
        if isinstance(failed_module, dict):
            try:
                resume_module_index = int(failed_module.get("index"))
                interval_failure = bool(failed_module.get("interval", False))
            except (TypeError, ValueError):
                resume_module_index = None

        failed_step_index = failed_step.get("index")
        if resume_module_index is None and isinstance(failed_step_index, int):
            if 0 <= failed_step_index < len(steps) and isinstance(steps[failed_step_index], dict):
                source_step = steps[failed_step_index]
                try:
                    resume_module_index = int(source_step.get("_module_index"))
                    interval_failure = bool(source_step.get("_composition_interval", False))
                except (TypeError, ValueError):
                    resume_module_index = None
        if resume_module_index is None or resume_module_index < 0:
            return None
        if interval_failure:
            resume_module_index += 1

        remaining_steps: list[dict[str, Any]] = []
        remaining_step_positions: dict[int, int] = {}
        for source_step_position, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            try:
                module_index = int(step.get("_module_index"))
            except (TypeError, ValueError):
                continue
            # The start module establishes the phone/app baseline and must run
            # on every retry. Only completed process modules are omitted.
            if module_index == 0 or module_index >= resume_module_index:
                remaining_step_positions[source_step_position] = len(remaining_steps) + 1
                remaining_steps.append(step)
        if not remaining_steps:
            return None

        source_composition = document.get("composition")
        if not isinstance(source_composition, list):
            source_composition = task.get("composition")
        if not isinstance(source_composition, list) or not source_composition:
            return None

        retry_composition: list[dict[str, Any]] = []
        skipped_modules: list[dict[str, Any]] = []
        resume_position = resume_module_index + 1
        for raw_module in source_composition:
            if not isinstance(raw_module, dict):
                continue
            module = dict(raw_module)
            try:
                position = int(module.get("position"))
            except (TypeError, ValueError):
                continue
            module["retry_skipped"] = 1 < position < resume_position
            module["retry_replayed"] = position == 1 and resume_position > 1
            module["retry_resume"] = position == resume_position
            if module["retry_skipped"]:
                skipped_modules.append({
                    "position": position,
                    "script_name": str(module.get("script_name") or ""),
                })
            retry_composition.append(module)
        if not any(module.get("retry_resume") for module in retry_composition):
            return None

        remaining_step_ids = {
            str(step.get("id"))
            for step in remaining_steps
            if step.get("id") is not None
        }
        remaining_popups: list[dict[str, Any]] = []
        for raw_popup in document.get("global_popups") or []:
            if not isinstance(raw_popup, dict):
                continue
            popup = dict(raw_popup)
            popup_module_index = popup.get("_module_index")
            popup_index: int | None = None
            if popup_module_index is not None:
                try:
                    popup_index = int(popup_module_index)
                    if popup_index != 0 and popup_index < resume_module_index:
                        continue
                except (TypeError, ValueError):
                    pass
            step_ids = popup.get("step_ids")
            if isinstance(step_ids, list):
                filtered_ids = [
                    step_id for step_id in step_ids
                    if str(step_id) in remaining_step_ids
                ]
                if step_ids and not filtered_ids:
                    continue
                popup["step_ids"] = filtered_ids
            raw_step_indexes = popup.get("step_indexes")
            if isinstance(raw_step_indexes, list):
                remapped_indexes: list[int] = []
                for raw_step_index in raw_step_indexes:
                    try:
                        source_step_position = int(raw_step_index)
                    except (TypeError, ValueError):
                        continue
                    retry_step_position = remaining_step_positions.get(source_step_position)
                    if retry_step_position is not None and retry_step_position not in remapped_indexes:
                        remapped_indexes.append(retry_step_position)
                if raw_step_indexes and not remapped_indexes:
                    continue
                popup["step_indexes"] = remapped_indexes
            elif raw_step_indexes is None and popup_index is not None:
                popup["step_indexes"] = [
                    retry_step_position
                    for source_step_position, retry_step_position in remaining_step_positions.items()
                    if int(steps[source_step_position - 1].get("_module_index", -1)) == popup_index
                ]
                if not popup["step_indexes"]:
                    continue
            remaining_popups.append(popup)

        document["steps"] = remaining_steps
        document["global_popups"] = remaining_popups
        document["composition"] = retry_composition
        document["composition_resume"] = {
            "from_module_index": resume_module_index,
            "from_position": resume_position,
            "failed_task_id": task.get("id"),
            "skipped_modules": skipped_modules,
            "replayed_start": resume_module_index > 0,
        }
        return json.dumps(document, ensure_ascii=False), retry_composition

    def enqueue_retry(
        self,
        failed_task_id: str,
        *,
        manual: bool = False,
        script_override: str | None = None,
        script_name_override: str | None = None,
        composition_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        with self._command_condition:
            with self.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (failed_task_id,)).fetchone()
                task = self.decode(row)
                if not task or task["status"] != "failed":
                    return None
                attempt = int(task.get("attempt") or 1)
                max_retries = int(task.get("max_retries") or 0)
                existing = db.execute(
                    "SELECT * FROM tasks WHERE retry_of_task_id=? ORDER BY created_at LIMIT 1",
                    (failed_task_id,),
                ).fetchone()
                if existing:
                    return self.decode(existing)
                if not manual and attempt > max_retries:
                    return None

                root_id = task.get("root_task_id") or task["id"]
                root_row = db.execute("SELECT name FROM tasks WHERE id=?", (root_id,)).fetchone()
                root_name = root_row["name"] if root_row else task["name"]
                retry_number = attempt
                retry_limit = max(max_retries, retry_number) if manual else max_retries
                retry_id = str(uuid.uuid4())
                retry_source = dict(task)
                if script_override is not None:
                    retry_source["script"] = script_override
                if script_name_override is not None:
                    retry_source["script_name"] = script_name_override
                if composition_override is not None:
                    retry_source["composition"] = composition_override
                retry_task = {
                    **retry_source,
                    "id": retry_id,
                    "name": f"{root_name} · 重试 {retry_number}/{retry_limit}",
                    "status": "queued",
                    "result": {},
                    "schedule_type": "retry",
                    "scheduled_for": None,
                    "schedule_window_end": None,
                    "root_task_id": root_id,
                    "retry_of_task_id": failed_task_id,
                    "attempt": attempt + 1,
                    "recording_path": None,
                    "recording_mime": None,
                    "recording_size": None,
                    "created_at": utc_now(),
                    "started_at": None,
                    "finished_at": None,
                }
                composition_retry = self._composition_retry_payload(retry_source)
                if (
                    composition_retry is None
                    and task.get("task_kind") == "composition"
                    and isinstance(task.get("result"), dict)
                    and not task["result"].get("steps")
                    and not task["result"].get("failed_step")
                    and task.get("retry_of_task_id")
                ):
                    parent_row = db.execute(
                        "SELECT * FROM tasks WHERE id=?",
                        (task["retry_of_task_id"],),
                    ).fetchone()
                    parent_task = self.decode(parent_row)
                    if parent_task:
                        checkpoint_source = dict(retry_source)
                        checkpoint_source["result"] = parent_task.get("result") or {}
                        composition_retry = self._composition_retry_payload(checkpoint_source)
                if composition_retry is not None:
                    retry_task["script"], retry_task["composition"] = composition_retry
                self._insert_task(db, retry_task)
                self._insert_command(db, {
                    "id": str(uuid.uuid4()),
                    "agent_id": retry_task["agent_id"],
                    "kind": "task",
                    "payload": self.task_command_payload(retry_task),
                })
            self._command_condition.notify_all()
        return self.get_task(retry_id)

    def set_task_recording(self, task_id: str, path: str, mime: str, size: int):
        with self.connect() as db:
            db.execute("""UPDATE tasks SET recording_path=?,recording_mime=?,recording_size=?
                WHERE id=?""", (path, mime, size, task_id))
        return self.get_task(task_id)

    def merge_task_result(self, task_id: str, values: dict[str, Any]):
        with self.connect() as db:
            row = db.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            try:
                result = json.loads(row["result"])
            except (TypeError, json.JSONDecodeError):
                result = {}
            result.update(values)
            db.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result), task_id))
        return self.get_task(task_id)

    def poll_commands(self, agent_id: str, limit: int = 10, accept_tasks: bool = True):
        with self.connect() as db:
            task_filter = "" if accept_tasks else "AND kind NOT IN ('task', 'run_script', 'run_step', 'quick_test')"
            rows = db.execute(f"""SELECT * FROM commands
                WHERE agent_id=? AND status='pending' {task_filter}
                ORDER BY CASE WHEN kind IN ('agent_start', 'agent_stop') THEN 0 ELSE 1 END,
                         created_at
                LIMIT ?""", (agent_id, limit)).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                db.executemany("UPDATE commands SET status='claimed', claimed_at=? WHERE id=?", [(utc_now(), i) for i in ids])
                task_ids = [json.loads(row["payload"]).get("task_id") for row in rows]
                db.executemany("UPDATE tasks SET status='running', started_at=? WHERE id=? AND status='queued'",
                               [(utc_now(), task_id) for task_id in task_ids if task_id])
        return [self.decode(row) for row in rows]

    def wait_for_commands(
        self,
        agent_id: str,
        timeout_seconds: float,
        limit: int = 10,
        accept_tasks: bool = True,
    ):
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._command_condition:
            while True:
                commands = self.poll_commands(agent_id, limit, accept_tasks=accept_tasks)
                if commands:
                    return commands
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._command_condition.wait(remaining)

    def complete_command(self, command_id: str, result: dict[str, Any], success: bool):
        with self._command_condition:
            with self.connect() as db:
                row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE commands SET status=?, finished_at=?, result=? WHERE id=?",
                           ("succeeded" if success else "failed", utc_now(), json.dumps(result), command_id))
                payload = json.loads(row["payload"])
                task_id = payload.get("task_id")
                if task_id:
                    db.execute("""UPDATE tasks SET status=?, result=?, finished_at=?
                        WHERE id=?""", ("succeeded" if success else "failed", json.dumps(result), utc_now(), task_id))
            self._command_condition.notify_all()
        return {"id": command_id, "success": success, "result": result}

    def update_feedback_email(
        self,
        command_id: str,
        feedback_id: str,
        status: str,
        error: str | None = None,
        recipients: list[str] | None = None,
    ) -> dict[str, Any] | None:
        with self._command_condition:
            with self.connect() as db:
                row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
                if row is None:
                    return None
                try:
                    result = json.loads(row["result"])
                except (TypeError, json.JSONDecodeError):
                    result = {}
                updated = False
                for step in result.get("steps", []):
                    if not isinstance(step, dict):
                        continue
                    step_result = step.get("result")
                    feedback = step_result.get("feedback") if isinstance(step_result, dict) else None
                    if not isinstance(feedback, dict) or feedback.get("id") != feedback_id:
                        continue
                    feedback["email_status"] = status
                    feedback["email_error"] = error
                    if recipients is not None:
                        feedback["recipients"] = recipients
                    updated = True
                    break
                if not updated:
                    return self.decode(row)
                encoded = json.dumps(result)
                db.execute("UPDATE commands SET result=? WHERE id=?", (encoded, command_id))
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                task_id = payload.get("task_id")
                if task_id:
                    db.execute("UPDATE tasks SET result=? WHERE id=?", (encoded, task_id))
            self._command_condition.notify_all()
        return self.get_command(command_id)

    def get_command(self, command_id: str):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())

    def wait_for_command(self, command_id: str, timeout_seconds: float):
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._command_condition:
            while True:
                command = self.get_command(command_id)
                if command is None or command.get("status") in {"succeeded", "failed", "cancelled"}:
                    return command
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return command
                self._command_condition.wait(remaining)

    def start_task(self, task_id: str):
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='running', started_at=? WHERE id=? AND status='queued'", (utc_now(), task_id))

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        """Cancel a task that has not been claimed by a terminal yet."""
        with self._command_condition:
            with self.connect() as db:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task is None:
                    return None
                if task["status"] not in {"queued", "scheduled"}:
                    return self.decode(task)

                now = utc_now()
                result = {"cancelled": True, "reason": "cancelled before terminal execution"}
                db.execute(
                    "UPDATE tasks SET status='cancelled', result=?, finished_at=? WHERE id=? AND status IN ('queued','scheduled')",
                    (json.dumps(result), now, task_id),
                )
                command_rows = db.execute(
                    "SELECT id,payload FROM commands WHERE agent_id=? AND status='pending'",
                    (task["agent_id"],),
                ).fetchall()
                command_ids = []
                for command in command_rows:
                    try:
                        if json.loads(command["payload"]).get("task_id") == task_id:
                            command_ids.append(command["id"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                if command_ids:
                    db.executemany(
                        "UPDATE commands SET status='cancelled', finished_at=?, result=? WHERE id=? AND status='pending'",
                        [(now, json.dumps(result), command_id) for command_id in command_ids],
                    )
                updated = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._command_condition.notify_all()
        return self.decode(updated)

    def delete_task(self, task_id: str) -> dict[str, Any] | None:
        """Delete one finished task and its command history."""
        with self._command_condition:
            with self.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    return None
                task = self.decode(row)
                if task["status"] not in {"succeeded", "failed", "cancelled"}:
                    return task
                command_ids = []
                for command in db.execute("SELECT id,payload FROM commands").fetchall():
                    try:
                        if json.loads(command["payload"]).get("task_id") == task_id:
                            command_ids.append(command["id"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                if command_ids:
                    db.executemany("DELETE FROM commands WHERE id=?", [(item,) for item in command_ids])
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._command_condition.notify_all()
        return task

    def maintenance_stats(self) -> dict[str, int]:
        with self.connect() as db:
            task_row = db.execute("""SELECT COUNT(*) AS total,
                SUM(CASE WHEN status IN ('succeeded','failed','cancelled') THEN 1 ELSE 0 END) AS finished,
                COALESCE(SUM(recording_size),0) AS recording_bytes,
                SUM(CASE WHEN recording_path IS NOT NULL THEN 1 ELSE 0 END) AS recordings
                FROM tasks""").fetchone()
            failure_row = db.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(screenshot_size),0) AS screenshot_bytes FROM failure_records""").fetchone()
        return {
            "tasks": int(task_row["total"] or 0),
            "finished_tasks": int(task_row["finished"] or 0),
            "recordings": int(task_row["recordings"] or 0),
            "recording_bytes": int(task_row["recording_bytes"] or 0),
            "failure_records": int(failure_row["total"] or 0),
            "failure_screenshot_bytes": int(failure_row["screenshot_bytes"] or 0),
        }

    def recording_cleanup_candidates(self, cutoff: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = """SELECT id,recording_path,recording_size FROM tasks
                WHERE recording_path IS NOT NULL
                AND status IN ('succeeded','failed','cancelled')"""
            values: tuple[Any, ...] = ()
            if cutoff:
                query += " AND COALESCE(finished_at,created_at)<?"
                values = (cutoff,)
            return [dict(row) for row in db.execute(query, values)]

    def clear_task_recordings(self, task_ids: list[str]):
        if not task_ids:
            return
        with self.connect() as db:
            db.executemany(
                "UPDATE tasks SET recording_path=NULL,recording_mime=NULL,recording_size=NULL WHERE id=?",
                [(task_id,) for task_id in task_ids],
            )

    def delete_task_history(
        self,
        cutoff: str | None = None,
        include_failure_evidence: bool = False,
    ) -> dict[str, Any]:
        with self._command_condition:
            with self.connect() as db:
                query = "SELECT * FROM tasks WHERE status IN ('succeeded','failed','cancelled')"
                values: tuple[Any, ...] = ()
                if cutoff:
                    query += " AND COALESCE(finished_at,created_at)<?"
                    values = (cutoff,)
                task_rows = db.execute(query, values).fetchall()
                task_ids = {row["id"] for row in task_rows}
                recording_paths = [row["recording_path"] for row in task_rows if row["recording_path"]]

                command_ids: list[str] = []
                if task_ids:
                    for command in db.execute("SELECT id,payload FROM commands").fetchall():
                        try:
                            if json.loads(command["payload"]).get("task_id") in task_ids:
                                command_ids.append(command["id"])
                        except (TypeError, json.JSONDecodeError):
                            continue
                    if command_ids:
                        db.executemany("DELETE FROM commands WHERE id=?", [(item,) for item in command_ids])
                    db.executemany("DELETE FROM tasks WHERE id=?", [(item,) for item in task_ids])

                failure_paths: list[str] = []
                failure_count = 0
                if include_failure_evidence and task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    failure_rows = db.execute(
                        f"SELECT id,screenshot_path FROM failure_records WHERE task_id IN ({placeholders})",
                        tuple(task_ids),
                    ).fetchall()
                    failure_paths = [row["screenshot_path"] for row in failure_rows if row["screenshot_path"]]
                    failure_count = len(failure_rows)
                    db.executemany("DELETE FROM failure_records WHERE id=?", [(row["id"],) for row in failure_rows])
            self._command_condition.notify_all()
        return {
            "tasks_deleted": len(task_ids),
            "commands_deleted": len(command_ids),
            "recording_paths": recording_paths,
            "failure_records_deleted": failure_count,
            "failure_paths": failure_paths,
        }

    def update_storage_status(
        self,
        scope: str,
        label: str,
        free_bytes: int,
        total_bytes: int,
        threshold_bytes: int,
        reminder_seconds: int = 24 * 3600,
    ) -> dict[str, Any]:
        now = utc_now()
        active = free_bytes < threshold_bytes
        with self.connect() as db:
            previous = db.execute("SELECT * FROM storage_alerts WHERE scope=?", (scope,)).fetchone()
            should_alert = False
            if active:
                if not previous or not bool(previous["active"]) or not previous["last_sent"]:
                    last_attempt = previous["last_attempt"] if previous else None
                    if not last_attempt:
                        should_alert = True
                    else:
                        try:
                            attempted_at = datetime.fromisoformat(last_attempt)
                            should_alert = (datetime.now(timezone.utc) - attempted_at).total_seconds() >= 3600
                        except (TypeError, ValueError):
                            should_alert = True
                else:
                    try:
                        last_sent = datetime.fromisoformat(previous["last_sent"])
                        should_alert = (datetime.now(timezone.utc) - last_sent).total_seconds() >= reminder_seconds
                    except (TypeError, ValueError):
                        should_alert = True
            last_sent = previous["last_sent"] if previous and active else None
            last_attempt = previous["last_attempt"] if previous and active else None
            last_error = previous["last_error"] if previous and active else None
            db.execute("""INSERT INTO storage_alerts(
                    scope,label,free_bytes,total_bytes,active,last_seen,last_sent,last_attempt,last_error
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scope) DO UPDATE SET
                    label=excluded.label,free_bytes=excluded.free_bytes,total_bytes=excluded.total_bytes,
                    active=excluded.active,last_seen=excluded.last_seen,last_sent=excluded.last_sent,
                    last_attempt=excluded.last_attempt,last_error=excluded.last_error""", (
                scope, label, int(free_bytes), int(total_bytes), int(active), now, last_sent, last_attempt, last_error,
            ))
        return {
            "scope": scope,
            "label": label,
            "free_bytes": int(free_bytes),
            "total_bytes": int(total_bytes),
            "active": active,
            "should_alert": should_alert,
            "last_seen": now,
            "last_sent": last_sent,
            "last_attempt": last_attempt,
            "last_error": last_error,
        }

    def mark_storage_alert_sent(self, scope: str):
        sent_at = utc_now()
        with self.connect() as db:
            db.execute(
                "UPDATE storage_alerts SET last_sent=?,last_attempt=?,last_error=NULL WHERE scope=?",
                (sent_at, sent_at, scope),
            )
        return sent_at

    def mark_storage_alert_failed(self, scope: str, error: str):
        attempted_at = utc_now()
        with self.connect() as db:
            db.execute(
                "UPDATE storage_alerts SET last_attempt=?,last_error=? WHERE scope=?",
                (attempted_at, error[:1000], scope),
            )
        return attempted_at

    def list_storage_status(self):
        with self.connect() as db:
            return [
                {**dict(row), "active": bool(row["active"])}
                for row in db.execute("SELECT * FROM storage_alerts ORDER BY scope")
            ]

    def vacuum(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    def add_log(self, agent_id: str, level: str, message: str):
        with self.connect() as db:
            db.execute("INSERT INTO logs(agent_id,level,message,created_at) VALUES(?,?,?,?)",
                       (agent_id, level, message, utc_now()))

    def list_logs(self, agent_id: str, limit: int = 200):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM logs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit)).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _insert_script_audit(
        db: sqlite3.Connection,
        agent_id: str,
        script_name: str,
        operation: str,
        *,
        from_category: str | None = None,
        to_category: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ):
        db.execute(
            """INSERT INTO script_audit_logs(
                agent_id,script_name,operation,from_category,to_category,details,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                agent_id,
                script_name,
                operation,
                from_category,
                to_category,
                json.dumps(details or {}, ensure_ascii=False),
                created_at or utc_now(),
            ),
        )

    @staticmethod
    def _ensure_script_category(
        db: sqlite3.Connection,
        agent_id: str,
        package_name: str,
        display_name: str | None = None,
    ):
        now = utc_now()
        db.execute(
            """INSERT INTO script_categories(
                agent_id,package_name,display_name,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(agent_id,package_name) DO NOTHING""",
            (agent_id, package_name, display_name or package_name, now, now),
        )

    def save_script(
        self,
        agent_id: str,
        name: str,
        content: str,
        category_package: str | None = None,
        source_package: str | None = None,
        source_activity: str | None = None,
        audit_operation: str = "saved",
    ):
        category_package = category_package or None
        source_package = source_package or None
        source_activity = source_activity or None
        now = utc_now()
        with self.connect() as db:
            previous = db.execute(
                "SELECT * FROM scripts WHERE agent_id=? AND name=?",
                (agent_id, name),
            ).fetchone()
            if category_package:
                self._ensure_script_category(db, agent_id, category_package)
            db.execute(
                """INSERT INTO scripts(
                    agent_id,name,content,category_package,source_package,source_activity,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(agent_id,name) DO UPDATE SET
                    content=excluded.content,
                    category_package=excluded.category_package,
                    source_package=excluded.source_package,
                    source_activity=excluded.source_activity,
                    updated_at=excluded.updated_at""",
                (
                    agent_id,
                    name,
                    content,
                    category_package,
                    source_package,
                    source_activity,
                    now,
                ),
            )
            previous_category = previous["category_package"] if previous else None
            operation = audit_operation if audit_operation != "saved" else ("updated" if previous else "created")
            self._insert_script_audit(
                db,
                agent_id,
                name,
                operation,
                from_category=previous_category,
                to_category=category_package,
                details={
                    "source_package": source_package,
                    "source_activity": source_activity,
                },
                created_at=now,
            )
            if previous and previous_category != category_package:
                self._insert_script_audit(
                    db,
                    agent_id,
                    name,
                    "category_changed",
                    from_category=previous_category,
                    to_category=category_package,
                    details={"reason": "script_save"},
                    created_at=now,
                )
            row = db.execute(
                """SELECT scripts.*, script_categories.display_name AS category_name
                FROM scripts
                LEFT JOIN script_categories
                  ON script_categories.agent_id=scripts.agent_id
                 AND script_categories.package_name=scripts.category_package
                WHERE scripts.agent_id=? AND scripts.name=?""",
                (agent_id, name),
            ).fetchone()
        return dict(row)

    def get_script(self, agent_id: str, name: str):
        with self.connect() as db:
            row = db.execute(
                """SELECT scripts.*, script_categories.display_name AS category_name
                FROM scripts
                LEFT JOIN script_categories
                  ON script_categories.agent_id=scripts.agent_id
                 AND script_categories.package_name=scripts.category_package
                WHERE scripts.agent_id=? AND scripts.name=?""",
                (agent_id, name),
            ).fetchone()
        return dict(row) if row else None

    def list_scripts(self, agent_id: str):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT scripts.*, script_categories.display_name AS category_name
                    FROM scripts
                    LEFT JOIN script_categories
                      ON script_categories.agent_id=scripts.agent_id
                     AND script_categories.package_name=scripts.category_package
                    WHERE scripts.agent_id=?
                    ORDER BY COALESCE(script_categories.display_name, ''), scripts.name""",
                    (agent_id,),
                )
            ]

    def list_script_categories(self, agent_id: str):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT category.*, COUNT(scripts.name) AS script_count
                    FROM script_categories AS category
                    LEFT JOIN scripts
                      ON scripts.agent_id=category.agent_id
                     AND scripts.category_package=category.package_name
                    WHERE category.agent_id=?
                    GROUP BY category.agent_id, category.package_name
                    ORDER BY category.display_name, category.package_name""",
                    (agent_id,),
                )
            ]

    def rename_script_category(self, agent_id: str, package_name: str, display_name: str):
        now = utc_now()
        with self.connect() as db:
            self._ensure_script_category(db, agent_id, package_name)
            previous = db.execute(
                "SELECT * FROM script_categories WHERE agent_id=? AND package_name=?",
                (agent_id, package_name),
            ).fetchone()
            db.execute(
                """UPDATE script_categories SET display_name=?,updated_at=?
                WHERE agent_id=? AND package_name=?""",
                (display_name, now, agent_id, package_name),
            )
            if previous["display_name"] != display_name:
                self._insert_script_audit(
                    db,
                    agent_id,
                    "",
                    "category_renamed",
                    from_category=package_name,
                    to_category=package_name,
                    details={
                        "from_display_name": previous["display_name"],
                        "to_display_name": display_name,
                    },
                    created_at=now,
                )
            row = db.execute(
                "SELECT * FROM script_categories WHERE agent_id=? AND package_name=?",
                (agent_id, package_name),
            ).fetchone()
        return dict(row)

    def move_script_category(self, agent_id: str, name: str, category_package: str | None):
        category_package = category_package or None
        now = utc_now()
        with self.connect() as db:
            previous = db.execute(
                "SELECT * FROM scripts WHERE agent_id=? AND name=?",
                (agent_id, name),
            ).fetchone()
            if not previous:
                return None
            if category_package:
                self._ensure_script_category(db, agent_id, category_package)
            previous_category = previous["category_package"]
            if previous_category != category_package:
                db.execute(
                    """UPDATE scripts SET category_package=?,updated_at=?
                    WHERE agent_id=? AND name=?""",
                    (category_package, now, agent_id, name),
                )
                self._insert_script_audit(
                    db,
                    agent_id,
                    name,
                    "category_changed",
                    from_category=previous_category,
                    to_category=category_package,
                    details={"reason": "script_manager"},
                    created_at=now,
                )
            row = db.execute(
                """SELECT scripts.*, script_categories.display_name AS category_name
                FROM scripts
                LEFT JOIN script_categories
                  ON script_categories.agent_id=scripts.agent_id
                 AND script_categories.package_name=scripts.category_package
                WHERE scripts.agent_id=? AND scripts.name=?""",
                (agent_id, name),
            ).fetchone()
        return dict(row)

    def delete_script(self, agent_id: str, name: str):
        now = utc_now()
        with self.connect() as db:
            previous = db.execute(
                "SELECT * FROM scripts WHERE agent_id=? AND name=?",
                (agent_id, name),
            ).fetchone()
            if not previous:
                return None
            db.execute("DELETE FROM scripts WHERE agent_id=? AND name=?", (agent_id, name))
            self._insert_script_audit(
                db,
                agent_id,
                name,
                "deleted",
                from_category=previous["category_package"],
                details={
                    "source_package": previous["source_package"],
                    "source_activity": previous["source_activity"],
                },
                created_at=now,
            )
        return dict(previous)

    def list_script_audit(self, agent_id: str, limit: int = 200):
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM script_audit_logs
                WHERE agent_id=? ORDER BY id DESC LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item["details"])
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def create_failure(self, failure: dict[str, Any]):
        with self.connect() as db:
            db.execute("""INSERT INTO failure_records(
                id,command_id,task_id,agent_id,script_name,error,failed_step_index,
                failed_step_action,screenshot_path,screenshot_mime,screenshot_size,
                email_status,email_error,result,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                failure["id"], failure["command_id"], failure.get("task_id"), failure["agent_id"],
                failure.get("script_name", ""), failure["error"], failure.get("failed_step_index"),
                failure.get("failed_step_action"), failure.get("screenshot_path"),
                failure.get("screenshot_mime"), failure.get("screenshot_size"),
                failure.get("email_status", "disabled"), failure.get("email_error"),
                json.dumps(failure.get("result", {})), failure.get("created_at", utc_now()),
            ))
        return self.get_failure(failure["id"])

    def get_failure(self, failure_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM failure_records WHERE id=?", (failure_id,)).fetchone()
        return self.decode(row)

    def list_failures(self, agent_id: str | None = None, limit: int = 200):
        with self.connect() as db:
            query = "SELECT * FROM failure_records ORDER BY created_at DESC LIMIT ?"
            values: tuple[Any, ...] = (limit,)
            if agent_id:
                query = "SELECT * FROM failure_records WHERE agent_id=? ORDER BY created_at DESC LIMIT ?"
                values = (agent_id, limit)
            return [self.decode(row) for row in db.execute(query, values)]

    def update_failure_email(self, failure_id: str, status: str, error: str | None = None):
        with self.connect() as db:
            db.execute(
                "UPDATE failure_records SET email_status=?, email_error=? WHERE id=?",
                (status, error, failure_id),
            )
        return self.get_failure(failure_id)

    def confirm_failure(self, failure_id: str):
        with self.connect() as db:
            db.execute("UPDATE failure_records SET confirmed=1 WHERE id=?", (failure_id,))
        return self.get_failure(failure_id)

    def delete_failure(self, failure_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM failure_records WHERE id=?", (failure_id,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM failure_records WHERE id=?", (failure_id,))
        return self.decode(row)

    def get_notification_settings(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM notification_settings WHERE id=1").fetchone()
        if not row:
            return None
        value = dict(row)
        try:
            value["failure_recipients"] = json.loads(value["failure_recipients"])
        except (TypeError, json.JSONDecodeError):
            value["failure_recipients"] = []
        value["smtp_starttls"] = bool(value["smtp_starttls"])
        value["smtp_ssl"] = bool(value["smtp_ssl"])
        value["failure_enabled"] = bool(value["failure_enabled"])
        return value

    def save_notification_settings(self, value: dict[str, Any]):
        now = utc_now()
        with self.connect() as db:
            db.execute("""INSERT INTO notification_settings(
                id,smtp_host,smtp_port,smtp_user,smtp_password_encrypted,smtp_from,
                smtp_starttls,smtp_ssl,failure_recipients,failure_enabled,public_base_url,updated_at
            ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                smtp_host=excluded.smtp_host,
                smtp_port=excluded.smtp_port,
                smtp_user=excluded.smtp_user,
                smtp_password_encrypted=excluded.smtp_password_encrypted,
                smtp_from=excluded.smtp_from,
                smtp_starttls=excluded.smtp_starttls,
                smtp_ssl=excluded.smtp_ssl,
                failure_recipients=excluded.failure_recipients,
                failure_enabled=excluded.failure_enabled,
                public_base_url=excluded.public_base_url,
                updated_at=excluded.updated_at""", (
                value.get("smtp_host", ""), int(value.get("smtp_port", 587)),
                value.get("smtp_user", ""), value.get("smtp_password_encrypted", ""),
                value.get("smtp_from", ""), int(bool(value.get("smtp_starttls", True))),
                int(bool(value.get("smtp_ssl", False))),
                json.dumps(value.get("failure_recipients", [])),
                int(bool(value.get("failure_enabled", False))),
                value.get("public_base_url", ""), now,
            ))
        return self.get_notification_settings()


store = Store()

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.store import Store


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "scheduler.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def task(task_id: str, name: str, **values):
        return {
            "id": task_id,
            "agent_id": "agent-1",
            "name": name,
            "script": '{"version":1,"steps":[]}',
            "params": {},
            **values,
        }

    def command(self, command_id: str, task: dict, created_at: str):
        return {
            "id": command_id,
            "agent_id": "agent-1",
            "kind": "task",
            "payload": self.store.task_command_payload(task),
            "created_at": created_at,
        }

    def test_failed_retry_is_appended_after_existing_fifo_tasks(self):
        first_time = datetime.now(timezone.utc)
        first = self.task(
            "task-a", "A", max_retries=1,
            created_at=first_time.isoformat(),
        )
        second = self.task(
            "task-b", "B",
            created_at=(first_time + timedelta(microseconds=1)).isoformat(),
        )
        self.store.create_task_batch([
            first, second,
        ], [
            self.command("command-a", first, first["created_at"]),
            self.command("command-b", second, second["created_at"]),
        ])

        claimed = self.store.poll_commands("agent-1", limit=1)
        self.assertEqual(claimed[0]["id"], "command-a")
        self.store.complete_command("command-a", {"error": "expected"}, False)
        retry = self.store.enqueue_retry("task-a")

        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["retry_of_task_id"], "task-a")
        next_command = self.store.poll_commands("agent-1", limit=1)
        self.assertEqual(next_command[0]["id"], "command-b")
        self.store.complete_command("command-b", {"ok": True}, True)
        retry_command = self.store.poll_commands("agent-1", limit=1)
        self.assertEqual(retry_command[0]["payload"]["task_id"], retry["id"])

    def test_manual_retry_works_without_a_configured_retry_budget(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task(
            "manual-retry",
            "Manual retry",
            max_retries=0,
            created_at=created,
        )
        self.store.create_task(task, self.command("manual-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command("manual-command", {"error": "expected"}, False)

        self.assertIsNone(self.store.enqueue_retry(task["id"]))
        retry = self.store.enqueue_retry(task["id"], manual=True)

        self.assertIsNotNone(retry)
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["max_retries"], 0)
        self.assertIn("重试 1/1", retry["name"])
        self.assertEqual(self.store.enqueue_retry(task["id"], manual=True)["id"], retry["id"])

    def test_finished_task_can_be_deleted_with_its_command_history(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task(
            "finished-task",
            "Finished task",
            status="succeeded",
            finished_at=created,
            recording_path="/managed/recording.mp4",
        )
        self.store.create_task(task, self.command("finished-command", task, created))

        deleted = self.store.delete_task(task["id"])

        self.assertEqual(deleted["id"], task["id"])
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertIsNone(self.store.get_command("finished-command"))

    def test_active_task_cannot_be_deleted(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task("queued-task", "Queued task", status="queued", created_at=created)
        self.store.create_task(task, self.command("queued-command", task, created))

        unchanged = self.store.delete_task(task["id"])

        self.assertEqual(unchanged["status"], "queued")
        self.assertIsNotNone(self.store.get_task(task["id"]))

    def test_retry_uses_the_refreshed_script_snapshot_when_provided(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task(
            "refresh-retry",
            "Current script",
            script_name="current.json",
            script='{"version":2,"steps":[{"action":"old"}]}',
            created_at=created,
        )
        self.store.create_task(task, self.command("refresh-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command("refresh-command", {"error": "expected"}, False)

        refreshed = '{"version":2,"steps":[{"action":"new"}]}'
        retry = self.store.enqueue_retry(
            task["id"],
            manual=True,
            script_override=refreshed,
        )

        self.assertEqual(retry["script"], refreshed)
        self.assertEqual(retry["script_name"], "current.json")

    def test_composition_retry_replays_start_and_skips_completed_process_modules(self):
        created = datetime.now(timezone.utc).isoformat()
        composition = [
            {"position": 1, "script_name": "开始.json", "script_type": "module_start"},
            {"position": 2, "script_name": "过程.json", "script_type": "module_process"},
            {
                "position": 3,
                "script_name": "结束.json",
                "script_type": "module_process",
                "cleanup_on_finish": True,
            },
        ]
        script = {
            "version": 2,
            "script_type": "composition",
            "composition": composition,
            "global_popups": [
                {"name": "开始弹窗", "step_ids": ["start"], "_module_index": 0},
                {"name": "过程弹窗", "step_ids": ["process"], "_module_index": 1},
                {"name": "结束弹窗", "step_ids": ["cleanup"], "_module_index": 2},
            ],
            "steps": [
                {"id": "start", "action": "start", "_module_index": 0, "_module_name": "开始.json", "_module_step_index": 0},
                {"id": "launch", "action": "launch_app", "package": "com.example.app", "_module_index": 0, "_module_name": "开始.json", "_module_step_index": 1},
                {"id": "process", "action": "wait", "seconds": 0, "_module_index": 1, "_module_name": "过程.json", "_module_step_index": 0},
                {"id": "cleanup", "action": "home", "_module_index": 2, "_module_name": "结束.json", "_module_step_index": 0},
            ],
        }
        script["global_popups"][2]["step_indexes"] = [4]
        task = self.task(
            "composition-a",
            "组合回归",
            script=json.dumps(script, ensure_ascii=False),
            task_kind="composition",
            composition=composition,
            max_retries=2,
            created_at=created,
        )
        self.store.create_task(task, self.command("composition-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command(
            "composition-command",
            {
                "error": "module failed",
                "composition_retry": {"retry_pending": True, "resume_available": True},
                "failed_step": {
                    "index": 2,
                    "module": {"index": 1, "name": "过程.json", "step_index": 0},
                },
            },
            False,
        )

        retry = self.store.enqueue_retry("composition-a")

        self.assertEqual(retry["task_kind"], "composition")
        self.assertEqual(retry["attempt"], 2)
        retry_script = json.loads(retry["script"])
        self.assertEqual(
            [step["_module_index"] for step in retry_script["steps"]],
            [0, 0, 1, 2],
        )
        self.assertEqual(retry_script["steps"][0]["action"], "start")
        self.assertEqual(retry_script["composition_resume"]["from_position"], 2)
        self.assertTrue(retry_script["composition_resume"]["replayed_start"])
        self.assertEqual(
            [popup["name"] for popup in retry_script["global_popups"]],
            ["开始弹窗", "过程弹窗", "结束弹窗"],
        )
        self.assertEqual(retry_script["global_popups"][2]["step_indexes"], [4])
        self.assertFalse(retry["composition"][0]["retry_skipped"])
        self.assertTrue(retry["composition"][0]["retry_replayed"])
        self.assertTrue(retry["composition"][1]["retry_resume"])
        self.assertFalse(retry["composition"][2]["retry_skipped"])
        command = self.store.poll_commands("agent-1", limit=1)[0]
        self.assertEqual(command["payload"]["task_kind"], "composition")
        self.assertEqual(command["payload"]["script"], retry["script"])
        self.assertEqual(command["payload"]["composition"], retry["composition"])

        self.store.complete_command(
            command["id"],
            {
                "error": "cleanup module failed",
                "composition_retry": {"retry_pending": True, "resume_available": True},
                "failed_step": {
                    "index": 3,
                    "module": {"index": 2, "name": "结束.json", "step_index": 0},
                },
            },
            False,
        )
        second_retry = self.store.enqueue_retry(retry["id"])
        second_script = json.loads(second_retry["script"])
        second_cleanup_popup = next(
            popup for popup in second_script["global_popups"]
            if popup.get("_module_index") == 2
        )
        self.assertEqual(second_cleanup_popup["step_indexes"], [3])
        self.assertEqual(
            [step["_module_index"] for step in second_script["steps"]],
            [0, 0, 2],
        )
        self.assertEqual(second_script["composition_resume"]["from_position"], 3)
        self.assertTrue(second_retry["composition"][0]["retry_replayed"])
        self.assertFalse(second_retry["composition"][0]["retry_skipped"])
        self.assertTrue(second_retry["composition"][1]["retry_skipped"])
        self.assertTrue(second_retry["composition"][2]["retry_resume"])

    def test_preflight_failed_retry_reuses_parent_composition_checkpoint(self):
        created = datetime.now(timezone.utc).isoformat()
        composition = [
            {"position": 1, "script_name": "start.json", "script_type": "module_start"},
            {"position": 2, "script_name": "process.json", "script_type": "module_process"},
            {"position": 3, "script_name": "cleanup.json", "script_type": "module_process", "cleanup_on_finish": True},
        ]
        script = {
            "version": 2,
            "script_type": "composition",
            "composition": composition,
            "global_popups": [{
                "name": "cleanup-popup",
                "step_indexes": [4],
                "_module_index": 2,
            }],
            "steps": [
                {"id": "start", "action": "start", "_module_index": 0},
                {"id": "launch", "action": "launch_app", "package": "com.example.app", "_module_index": 0},
                {"id": "process", "action": "wait", "seconds": 0, "_module_index": 1},
                {"id": "cleanup", "action": "home", "_module_index": 2},
            ],
        }
        task = self.task(
            "composition-preflight",
            "Preflight retry",
            script=json.dumps(script),
            task_kind="composition",
            composition=composition,
            max_retries=2,
            created_at=created,
        )
        self.store.create_task(task, self.command("preflight-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command(
            "preflight-command",
            {
                "error": "module failed",
                "composition_retry": {"retry_pending": True, "resume_available": True},
                "failed_step": {"index": 2, "module": {"index": 1}},
            },
            False,
        )

        retry = self.store.enqueue_retry(task["id"])
        retry_command = self.store.poll_commands("agent-1", limit=1)[0]
        self.store.complete_command(
            retry_command["id"],
            {"error": "global popup 1 step index is out of range"},
            False,
        )

        recovered = self.store.enqueue_retry(retry["id"], manual=True)
        recovered_script = json.loads(recovered["script"])
        self.assertEqual(
            [step["_module_index"] for step in recovered_script["steps"]],
            [0, 0, 1, 2],
        )
        self.assertEqual(recovered_script["global_popups"][0]["step_indexes"], [4])

    def test_composition_retry_without_module_evidence_falls_back_to_full_script(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task(
            "composition-fallback",
            "兼容旧终端",
            task_kind="composition",
            composition=[
                {"position": 1, "script_name": "开始.json", "script_type": "module_start"},
                {"position": 2, "script_name": "结束.json", "script_type": "module_process", "cleanup_on_finish": True},
            ],
            max_retries=1,
            created_at=created,
        )
        self.store.create_task(task, self.command("fallback-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command(
            "fallback-command",
            {
                "error": "legacy failure",
                "failed_step": {
                    "index": 0,
                    "module": {"index": 0, "name": "开始.json", "step_index": 0},
                },
            },
            False,
        )

        retry = self.store.enqueue_retry(task["id"])

        self.assertEqual(retry["script"], task["script"])
        self.assertEqual(retry["composition"], task["composition"])

    def test_manual_composition_retry_uses_checkpoint_even_without_auto_retries(self):
        created = datetime.now(timezone.utc).isoformat()
        composition = [
            {"position": 1, "script_name": "开始.json", "script_type": "module_start"},
            {"position": 2, "script_name": "A.json", "script_type": "module_process"},
            {"position": 3, "script_name": "B.json", "script_type": "module_process", "cleanup_on_finish": True},
        ]
        script = {
            "version": 2,
            "script_type": "composition",
            "composition": composition,
            "steps": [
                {"id": "start", "action": "start", "_module_index": 0, "_module_name": "开始.json"},
                {"id": "a", "action": "wait", "seconds": 0, "_module_index": 1, "_module_name": "A.json"},
                {"id": "b", "action": "home", "_module_index": 2, "_module_name": "B.json"},
            ],
        }
        task = self.task(
            "composition-manual-checkpoint",
            "组合断点",
            script=json.dumps(script, ensure_ascii=False),
            task_kind="composition",
            composition=composition,
            max_retries=0,
            created_at=created,
        )
        self.store.create_task(task, self.command("manual-composition-command", task, created))
        self.store.poll_commands("agent-1", limit=1)
        self.store.complete_command(
            "manual-composition-command",
            {
                "error": "B failed",
                "composition_retry": {
                    "retry_pending": False,
                    "resume_available": False,
                    "phone_state_preserved": False,
                },
                "failed_step": {
                    "index": 2,
                    "module": {"index": 2, "name": "B.json", "step_index": 0},
                },
            },
            False,
        )

        retry = self.store.enqueue_retry(task["id"], manual=True)
        retry_script = json.loads(retry["script"])
        self.assertEqual([step["_module_index"] for step in retry_script["steps"]], [0, 2])
        self.assertTrue(retry["composition"][1]["retry_skipped"])
        self.assertTrue(retry["composition"][2]["retry_resume"])

    def test_feedback_email_status_updates_command_and_task_results(self):
        created = datetime.now(timezone.utc).isoformat()
        task = self.task("feedback-task", "feedback", created_at=created)
        self.store.create_task(task, self.command("feedback-command", task, created))
        result = {
            "steps": [{
                "index": 1,
                "action": "feedback",
                "result": {"feedback": {"id": "feedback-1", "email_status": "pending"}},
            }],
            "success": True,
        }
        self.store.complete_command("feedback-command", result, True)

        self.store.update_feedback_email(
            "feedback-command", "feedback-1", "sent", recipients=["owner@example.com"]
        )

        command = self.store.get_command("feedback-command")
        saved_task = self.store.get_task("feedback-task")
        command_feedback = command["result"]["steps"][0]["result"]["feedback"]
        task_feedback = saved_task["result"]["steps"][0]["result"]["feedback"]
        self.assertEqual(command_feedback["email_status"], "sent")
        self.assertEqual(command_feedback["recipients"], ["owner@example.com"])
        self.assertEqual(task_feedback["email_status"], "sent")

    def test_scheduled_task_is_only_queued_when_due(self):
        now = datetime.now(timezone.utc)
        task = self.task(
            "scheduled-task",
            "scheduled",
            status="scheduled",
            schedule_type="scheduled",
            scheduled_for=(now + timedelta(minutes=5)).isoformat(),
            created_at=now.isoformat(),
        )
        self.store.create_task(task)

        self.assertEqual(self.store.poll_commands("agent-1"), [])
        self.assertEqual(self.store.activate_due_tasks(now.isoformat()), [])
        activated = self.store.activate_due_tasks((now + timedelta(minutes=6)).isoformat())

        self.assertEqual([item["id"] for item in activated], ["scheduled-task"])
        command = self.store.poll_commands("agent-1", limit=1)
        self.assertEqual(command[0]["payload"]["task_id"], "scheduled-task")

    def test_storage_alert_is_edge_triggered_and_resets_after_recovery(self):
        threshold = 5 * 1024 ** 3
        low = self.store.update_storage_status("platform", "platform", threshold - 1, threshold * 2, threshold)
        self.assertTrue(low["should_alert"])
        self.store.mark_storage_alert_sent("platform")

        still_low = self.store.update_storage_status("platform", "platform", threshold - 2, threshold * 2, threshold)
        self.assertFalse(still_low["should_alert"])
        recovered = self.store.update_storage_status("platform", "platform", threshold + 1, threshold * 2, threshold)
        self.assertFalse(recovered["active"])
        low_again = self.store.update_storage_status("platform", "platform", threshold - 3, threshold * 2, threshold)
        self.assertTrue(low_again["should_alert"])

    def test_failed_storage_email_is_not_retried_on_every_heartbeat(self):
        threshold = 5 * 1024 ** 3
        low = self.store.update_storage_status("phone:agent-1", "phone", threshold - 1, threshold * 2, threshold)
        self.assertTrue(low["should_alert"])
        self.store.mark_storage_alert_failed("phone:agent-1", "SMTP unavailable")

        immediate = self.store.update_storage_status(
            "phone:agent-1", "phone", threshold - 2, threshold * 2, threshold,
        )
        self.assertFalse(immediate["should_alert"])
        self.assertEqual(immediate["last_error"], "SMTP unavailable")


if __name__ == "__main__":
    unittest.main()

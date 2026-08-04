import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server.emailer import SMTPConfiguration, send_failure_notification
from server.secret_store import SecretBox
from server.store import Store

try:
    from server import main as server_main
except ModuleNotFoundError:
    server_main = None


class FailureStoreTests(unittest.TestCase):
    def test_notification_settings_and_password_are_persisted_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(str(root / "control-center.db"))
            secret_box = SecretBox(root / "notification.key")
            encrypted = secret_box.encrypt("mail-app-password")
            saved = store.save_notification_settings({
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_user": "sender@example.com",
                "smtp_password_encrypted": encrypted,
                "smtp_from": "sender@example.com",
                "smtp_starttls": False,
                "smtp_ssl": True,
                "failure_recipients": ["owner@example.com"],
                "failure_enabled": True,
                "public_base_url": "http://127.0.0.1:8000",
            })

            self.assertNotIn("mail-app-password", saved["smtp_password_encrypted"])
            self.assertEqual(secret_box.decrypt(saved["smtp_password_encrypted"]), "mail-app-password")
            self.assertEqual(saved["failure_recipients"], ["owner@example.com"])
            self.assertTrue(saved["failure_enabled"])
            self.assertTrue((root / "notification.key").is_file())

    def test_failure_record_is_persisted_and_email_status_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(str(Path(temporary) / "control-center.db"))
            created = store.create_failure({
                "id": "failure-1",
                "command_id": "command-1",
                "agent_id": "agent-1",
                "script_name": "demo.json",
                "error": "target image timeout",
                "failed_step_index": 2,
                "failed_step_action": "wait_click",
                "email_status": "pending",
                "result": {"success": False},
            })

            self.assertEqual(created["failed_step_index"], 2)
            self.assertEqual(created["result"], {"success": False})
            self.assertFalse(created["confirmed"])
            self.assertEqual(store.overview()["failure_records"], 1)
            updated = store.update_failure_email("failure-1", "failed", "smtp offline")
            self.assertEqual(updated["email_status"], "failed")
            self.assertEqual(updated["email_error"], "smtp offline")

            confirmed = store.confirm_failure("failure-1")
            self.assertTrue(confirmed["confirmed"])
            self.assertEqual(store.overview()["failure_records"], 0)
            self.assertEqual(len(store.list_failures()), 1)

            deleted = store.delete_failure("failure-1")
            self.assertEqual(deleted["id"], "failure-1")
            self.assertEqual(store.list_failures(), [])
            self.assertEqual(store.overview()["failure_records"], 0)

    @unittest.skipIf(server_main is None, "FastAPI server dependencies are not installed in the host Python")
    def test_failure_screenshot_is_moved_out_of_command_json(self):
        screenshot_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as temporary:
            local_store = Store(str(Path(temporary) / "control-center.db"))
            result = {
                "error": "target image timeout",
                "failed_step": {
                    "index": 2,
                    "number": 3,
                    "action": "wait_click",
                    "module": {"index": 1, "name": "A.json", "step_index": 4, "interval": False},
                },
                "failure_screenshot": {
                    "mime": "image/png",
                    "size_bytes": len(screenshot_bytes),
                    "data_base64": base64.b64encode(screenshot_bytes).decode(),
                    "path": "/remote/agent/latest-screen.png",
                },
            }
            command = {
                "id": "command-1",
                "agent_id": "agent-1",
                "kind": "run_script",
                "payload": {"name": "demo.json"},
            }
            with (
                patch.object(server_main, "store", local_store),
                patch.object(server_main, "FAILURE_DIR", Path(temporary)),
                patch.object(server_main, "failure_notifications_configured", return_value=False),
            ):
                failure = server_main.create_failure_record(command, result)

            self.assertTrue(Path(failure["screenshot_path"]).is_file())
            self.assertNotIn("data_base64", failure["result"]["failure_screenshot"])
            self.assertNotIn("path", failure["result"]["failure_screenshot"])
            self.assertIn("failure_screenshot_url", result)
            self.assertEqual(failure["email_status"], "disabled")
            self.assertEqual(
                failure["result"]["failed_module"],
                {"position": 2, "name": "A.json", "step_number": 5, "interval": False},
            )

    @unittest.skipIf(server_main is None, "FastAPI server dependencies are not installed in the host Python")
    def test_success_feedback_capture_is_prepared_without_base64_in_result(self):
        screenshot_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        result = {
            "steps": [{
                "index": 1,
                "action": "feedback",
                "result": {"feedback": {
                    "subject": "daily result",
                    "message": "done",
                    "capture": {
                        "mime": "image/png",
                        "path": "/remote/latest-screen.png",
                        "data_base64": base64.b64encode(screenshot_bytes).decode(),
                    },
                }},
            }],
        }
        command = {
            "id": "command-feedback",
            "agent_id": "agent-1",
            "kind": "task",
            "payload": {"name": "demo.json", "task_id": "task-1"},
        }
        configuration = SMTPConfiguration(
            host="smtp.example.com",
            port=587,
            user="sender@example.com",
            password="secret",
            from_address="sender@example.com",
            starttls=True,
            ssl=False,
            failure_recipients=("owner@example.com",),
            failure_enabled=False,
            public_base_url="http://127.0.0.1:8000",
        )
        with tempfile.TemporaryDirectory() as temporary, (
            patch.object(server_main, "FEEDBACK_TEMP_DIR", Path(temporary))
        ), patch.object(server_main, "current_notification_configuration", return_value=configuration):
            jobs = server_main.prepare_success_feedbacks(command, result)
            feedback = result["steps"][0]["result"]["feedback"]
            self.assertEqual(feedback["email_status"], "pending")
            self.assertNotIn("data_base64", feedback["capture"])
            self.assertNotIn("path", feedback["capture"])
            self.assertEqual(len(jobs), 1)
            self.assertTrue(Path(jobs[0]["path"]).is_file())

    @unittest.skipIf(server_main is None, "FastAPI server dependencies are not installed in the host Python")
    def test_conditional_skip_notification_is_prepared_and_persisted(self):
        command = {
            "id": "command-skip",
            "agent_id": "agent-1",
            "kind": "task",
            "payload": {"name": "daily.json", "task_id": "task-1"},
        }
        result = {
            "conditional_skips": [{
                "trigger_step_index": 1,
                "trigger_step_number": 2,
                "trigger_action": "wait_click",
                "mode": "numeric",
                "operator": "gt",
                "value": 10,
                "scope": "remaining",
                "skipped_step_indexes": [1, 2, 3],
                "skipped_step_numbers": [2, 3, 4],
            }],
        }
        configuration = SMTPConfiguration(
            host="smtp.example.com",
            port=587,
            user="sender@example.com",
            password="secret",
            from_address="sender@example.com",
            starttls=True,
            ssl=False,
            failure_recipients=("owner@example.com",),
            failure_enabled=True,
            public_base_url="http://127.0.0.1:8000",
        )
        with tempfile.TemporaryDirectory() as temporary:
            local_store = Store(str(Path(temporary) / "control-center.db"))
            local_store.create_command(command)
            with patch.object(server_main, "store", local_store), patch.object(
                server_main,
                "current_notification_configuration",
                return_value=configuration,
            ):
                jobs = server_main.prepare_conditional_skip_notifications(command, result)

            event = result["conditional_skips"][0]
            self.assertEqual(event["email_status"], "pending")
            self.assertEqual(len(jobs), 1)
            local_store.complete_command(command["id"], result, True)
            updated = local_store.update_conditional_skip_email(
                command["id"],
                event["id"],
                "sent",
                recipients=["owner@example.com"],
            )
            persisted = updated["result"]["conditional_skips"][0]
            self.assertEqual(persisted["email_status"], "sent")
            self.assertEqual(persisted["recipients"], ["owner@example.com"])


class FailureEmailTests(unittest.TestCase):
    def test_failure_email_attaches_screenshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "failure.png"
            screenshot.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
            fake_settings = SimpleNamespace(
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_user="sender@example.com",
                smtp_password="app-password",
                smtp_from="sender@example.com",
                smtp_starttls=True,
                smtp_ssl=False,
                failure_email_to=("owner@example.com",),
                public_base_url="http://127.0.0.1:8000",
            )
            smtp = MagicMock()
            smtp.__enter__.return_value = smtp
            with patch("server.emailer.settings", fake_settings), patch("server.emailer.smtplib.SMTP", return_value=smtp):
                recipients = send_failure_notification({
                    "id": "failure-1",
                    "agent_id": "agent-1",
                    "script_name": "demo.json",
                    "error": "target image timeout",
                    "failed_step_index": 2,
                    "failed_step_action": "wait_click",
                    "created_at": "2026-07-29T00:00:00+00:00",
                    "screenshot_mime": "image/png",
                }, screenshot)

            self.assertEqual(recipients, ["owner@example.com"])
            smtp.starttls.assert_called_once_with()
            smtp.login.assert_called_once_with("sender@example.com", "app-password")
            message = smtp.send_message.call_args.args[0]
            self.assertEqual(len(list(message.iter_attachments())), 1)


if __name__ == "__main__":
    unittest.main()

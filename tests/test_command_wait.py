import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.store import Store, utc_now


class CommandWaitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "wait-test.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def command(command_id: str = "command-1"):
        return {
            "id": command_id,
            "agent_id": "agent-1",
            "kind": "detect_app",
            "payload": {},
            "created_at": utc_now(),
        }

    def test_waiting_agent_is_woken_when_command_is_created(self):
        result = []
        ready = threading.Event()

        def wait():
            ready.set()
            result.extend(self.store.wait_for_commands("agent-1", 2))

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(ready.wait(1))
        time.sleep(0.05)
        started = time.monotonic()
        self.store.create_command(self.command())
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([item["id"] for item in result], ["command-1"])

    def test_waiting_browser_is_woken_when_command_finishes(self):
        self.store.create_command(self.command())
        self.store.poll_commands("agent-1")
        result = []
        ready = threading.Event()

        def wait():
            ready.set()
            result.append(self.store.wait_for_command("command-1", 2))

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(ready.wait(1))
        time.sleep(0.05)
        started = time.monotonic()
        self.store.complete_command("command-1", {"detected": True}, True)
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result[0]["status"], "succeeded")
        self.assertTrue(result[0]["result"]["detected"])

    def test_service_commands_have_priority_and_paused_agent_does_not_claim_tasks(self):
        self.store.create_task(
            {
                "id": "task-1",
                "agent_id": "agent-1",
                "name": "queued test",
                "script": '{"steps": []}',
                "params": {},
            },
            {
                "id": "task-command",
                "agent_id": "agent-1",
                "kind": "task",
                "payload": {"task_id": "task-1"},
            },
        )
        self.store.create_command({
            "id": "stop-command",
            "agent_id": "agent-1",
            "kind": "agent_stop",
            "payload": {},
        })

        commands = self.store.poll_commands("agent-1", limit=1, accept_tasks=True)
        self.assertEqual([item["id"] for item in commands], ["stop-command"])
        self.assertEqual(self.store.get_task("task-1")["status"], "queued")
        self.assertEqual(self.store.poll_commands("agent-1", limit=1, accept_tasks=False), [])

        self.store.create_command({
            "id": "start-command",
            "agent_id": "agent-1",
            "kind": "agent_start",
            "payload": {},
        })
        commands = self.store.poll_commands("agent-1", limit=1, accept_tasks=False)
        self.assertEqual([item["id"] for item in commands], ["start-command"])

    def test_paused_agent_does_not_claim_quick_tests(self):
        self.store.create_command({
            "id": "quick-test-command",
            "agent_id": "agent-1",
            "kind": "quick_test",
            "payload": {"repeat_count": 1},
        })

        self.assertEqual(
            self.store.poll_commands("agent-1", limit=1, accept_tasks=False),
            [],
        )
        claimed = self.store.poll_commands("agent-1", limit=1, accept_tasks=True)
        self.assertEqual([item["id"] for item in claimed], ["quick-test-command"])

    def test_queued_task_can_be_cancelled_before_claim(self):
        self.store.create_task(
            {
                "id": "task-cancel",
                "agent_id": "agent-1",
                "name": "cancel me",
                "script": '{"steps": []}',
                "params": {},
            },
            {
                "id": "cancel-command",
                "agent_id": "agent-1",
                "kind": "task",
                "payload": {"task_id": "task-cancel"},
            },
        )

        task = self.store.cancel_task("task-cancel")

        self.assertEqual(task["status"], "cancelled")
        self.assertTrue(task["result"]["cancelled"])
        self.assertEqual(self.store.get_command("cancel-command")["status"], "cancelled")
        self.assertEqual(self.store.poll_commands("agent-1"), [])


if __name__ == "__main__":
    unittest.main()

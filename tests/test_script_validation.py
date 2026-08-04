import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from server import main as server_main
from server.main import compile_composition, resolve_failure_retry_scripts, validate_task_script
from server.schemas import TaskCompositionModule


class ScriptValidationTests(unittest.TestCase):
    def validate(self, post_assertion: object) -> dict:
        return validate_task_script(json.dumps({
            "version": 2,
            "steps": [{
                "action": "home",
                "post_assertion": post_assertion,
            }],
        }))

    def test_accepts_enabled_post_assertion(self):
        document = self.validate({
            "enabled": True,
            "template_base64": "data:image/png;base64,AA==",
            "threshold": 0.9,
            "timeout_seconds": 2,
            "poll_interval_seconds": 0.2,
            "max_retries": 3,
        })

        self.assertEqual(document["steps"][0]["post_assertion"]["max_retries"], 3)

    def test_requires_template_for_enabled_post_assertion(self):
        with self.assertRaises(HTTPException) as raised:
            self.validate({"enabled": True})

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("没有截取断言图片", raised.exception.detail)

    def test_rejects_fractional_post_assertion_retry_count(self):
        with self.assertRaises(HTTPException) as raised:
            self.validate({
                "enabled": True,
                "template_base64": "data:image/png;base64,AA==",
                "max_retries": 2.5,
            })

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("必须是 1 到 20 之间的整数", raised.exception.detail)

    def test_ignores_disabled_post_assertion_payload(self):
        document = self.validate({
            "enabled": False,
            "max_retries": "not-used",
        })

        self.assertFalse(document["steps"][0]["post_assertion"]["enabled"])

    def test_accepts_condition_skip_target_after_current_step(self):
        document = validate_task_script(json.dumps({
            "version": 2,
            "steps": [
                {"action": "home"},
                {
                    "action": "wait",
                    "seconds": 1,
                    "skip_condition": {
                        "enabled": True,
                        "skip_to_step_index": 3,
                    },
                },
                {"action": "back"},
            ],
        }))

        self.assertEqual(document["steps"][1]["skip_condition"]["skip_to_step_index"], 3)

    def test_rejects_condition_skip_target_before_or_at_current_step(self):
        with self.assertRaisesRegex(HTTPException, "后续已存在"):
            validate_task_script(json.dumps({
                "version": 2,
                "steps": [
                    {"action": "home"},
                    {
                        "action": "back",
                        "skip_condition": {
                            "enabled": True,
                            "skip_to_step_index": 2,
                        },
                    },
                ],
            }))

    def test_accepts_failure_retry_on_the_target_step(self):
        document = validate_task_script(json.dumps({
            "version": 2,
            "steps": [
                {"action": "home"},
                {
                    "action": "back",
                    "failure_retry": {
                        "enabled": True,
                        "max_retries": 3,
                        "process_script_name": "返回流程.json",
                    },
                },
            ],
        }))

        self.assertEqual(document["steps"][1]["failure_retry"]["max_retries"], 3)
        self.assertEqual(
            document["steps"][1]["failure_retry"]["process_script_name"],
            "返回流程.json",
        )

    def test_rejects_start_missing_script_and_fractional_failure_retry(self):
        invalid_steps = [
            [{
                "action": "start",
                "failure_retry": {
                    "enabled": True,
                    "max_retries": 2,
                    "process_script_name": "恢复.json",
                },
            }],
            [{
                "action": "back",
                "failure_retry": {"enabled": True, "max_retries": 2},
            }],
            [
                {"action": "home"},
                {
                    "action": "back",
                    "failure_retry": {
                        "enabled": True,
                        "max_retries": 1.5,
                        "process_script_name": "恢复.json",
                    },
                },
            ],
        ]
        for steps in invalid_steps:
            with self.subTest(steps=steps), self.assertRaises(HTTPException):
                validate_task_script(json.dumps({"version": 2, "steps": steps}))

    def test_resolves_latest_process_script_content_before_dispatch(self):
        process_content = json.dumps({
            "version": 2,
            "script_type": "module_process",
            "cleanup_on_finish": False,
            "steps": [{"action": "back"}],
        })
        main_content = json.dumps({
            "version": 2,
            "steps": [{
                "action": "home",
                "failure_retry": {
                    "enabled": True,
                    "max_retries": 2,
                    "process_script_name": "恢复.json",
                },
            }],
        })

        with patch.object(
            server_main.store,
            "get_script",
            return_value={"name": "恢复.json", "content": process_content},
        ):
            resolved = json.loads(resolve_failure_retry_scripts("agent-1", main_content))

        retry = resolved["steps"][0]["failure_retry"]
        self.assertEqual(retry["process_script"]["script_type"], "module_process")
        self.assertEqual(retry["process_script"]["steps"][0]["action"], "back")

    def test_rejects_cleanup_process_script_and_circular_reference(self):
        main_content = json.dumps({
            "version": 2,
            "script_type": "module_process",
            "steps": [{
                "action": "home",
                "failure_retry": {
                    "enabled": True,
                    "max_retries": 2,
                    "process_script_name": "恢复.json",
                },
            }],
        })
        cleanup_content = json.dumps({
            "version": 2,
            "script_type": "module_process",
            "cleanup_on_finish": True,
            "steps": [{"action": "back"}],
        })
        with patch.object(
            server_main.store,
            "get_script",
            return_value={"name": "恢复.json", "content": cleanup_content},
        ), self.assertRaisesRegex(HTTPException, "结束清理"):
            resolve_failure_retry_scripts("agent-1", main_content)

        with patch.object(
            server_main.store,
            "get_script",
            return_value={"name": "恢复.json", "content": main_content},
        ), self.assertRaisesRegex(HTTPException, "循环引用"):
            resolve_failure_retry_scripts(
                "agent-1",
                main_content,
                reference_chain=("恢复.json",),
            )


    def test_composition_offsets_module_popup_steps_and_defaults_to_module_scope(self):
        modules = [
            TaskCompositionModule(script_name="start.json"),
            TaskCompositionModule(script_name="activity.json"),
        ]
        documents = {
            "start.json": {
                "version": 2,
                "script_type": "module_start",
                "steps": [
                    {"action": "start"},
                    {"action": "launch_app", "package": "com.example.app"},
                ],
            },
            "activity.json": {
                "version": 2,
                "script_type": "module_process",
                "cleanup_on_finish": True,
                "global_popups": [
                    {
                        "name": "login-ticket",
                        "template_base64": "data:image/png;base64,AA==",
                        "click_mode": "match_center",
                        "step_indexes": [1, 3],
                    },
                    {
                        "name": "all-steps",
                        "template_base64": "data:image/png;base64,AA==",
                        "click_mode": "match_center",
                    },
                ],
                "steps": [
                    {"action": "wait", "seconds": 0},
                    {"action": "wait", "seconds": 0},
                    {"action": "wait", "seconds": 0},
                ],
            },
        }

        with patch.object(
            server_main.store,
            "get_script",
            side_effect=lambda _agent_id, name: {
                "name": name,
                "content": json.dumps(documents[name]),
            },
        ):
            compiled, _summary = compile_composition("agent-1", modules)

        popups = json.loads(compiled)["global_popups"]
        self.assertEqual(popups[0]["step_indexes"], [3, 5])
        self.assertEqual(popups[1]["step_indexes"], [3, 4, 5])


if __name__ == "__main__":
    unittest.main()

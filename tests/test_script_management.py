import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import main as server_main
from server.schemas import (
    ScriptCategoryMove,
    ScriptCategoryUpdate,
    ScriptImportPayload,
    ScriptPayload,
)
from server.store import Store


class ScriptManagementStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "script-management.db"

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def script(package: str | None = None, activity: str = ""):
        steps = [{"action": "start"}]
        if package:
            steps.append({
                "action": "launch_app",
                "package": package,
                "activity": activity,
            })
        return json.dumps({
            "version": 2,
            "script_type": "standard",
            "steps": steps,
        }, ensure_ascii=False)

    def test_old_script_table_is_migrated_and_application_is_backfilled(self):
        database = sqlite3.connect(self.database_path)
        try:
            database.execute(
                """CREATE TABLE scripts(
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, name)
                )"""
            )
            database.execute(
                "INSERT INTO scripts(agent_id,name,content,updated_at) VALUES(?,?,?,?)",
                (
                    "agent-1",
                    "国服.json",
                    self.script("com.RoamingStar.BlueArchive/.MainActivity"),
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            database.commit()
        finally:
            database.close()

        store = Store(str(self.database_path))
        item = store.get_script("agent-1", "国服.json")
        categories = store.list_script_categories("agent-1")

        self.assertEqual(item["category_package"], "com.RoamingStar.BlueArchive")
        self.assertEqual(item["source_activity"], ".MainActivity")
        self.assertEqual(categories[0]["display_name"], "com.RoamingStar.BlueArchive")
        self.assertEqual(categories[0]["script_count"], 1)

    def test_alias_move_delete_and_audit_history_are_persisted(self):
        store = Store(str(self.database_path))
        store.save_script(
            "agent-1",
            "国服.json",
            self.script("com.RoamingStar.BlueArchive", ".MainActivity"),
            "com.RoamingStar.BlueArchive",
            "com.RoamingStar.BlueArchive",
            ".MainActivity",
        )
        store.rename_script_category(
            "agent-1",
            "com.RoamingStar.BlueArchive",
            "蔚蓝档案（国服）",
        )
        moved = store.move_script_category("agent-1", "国服.json", None)
        deleted = store.delete_script("agent-1", "国服.json")
        audit = store.list_script_audit("agent-1")

        self.assertIsNone(moved["category_package"])
        self.assertEqual(deleted["name"], "国服.json")
        self.assertIsNone(store.get_script("agent-1", "国服.json"))
        self.assertEqual(
            [record["operation"] for record in audit],
            ["deleted", "category_changed", "category_renamed", "created"],
        )
        self.assertEqual(
            audit[1]["from_category"],
            "com.RoamingStar.BlueArchive",
        )


class ScriptManagementMetadataTests(unittest.TestCase):
    def test_component_input_is_split_into_package_and_activity(self):
        self.assertEqual(
            server_main.normalized_android_component(
                "com.RoamingStar.BlueArchive/com.tech.sdk.bridge.TechSDKUnityPlayerActivity",
            ),
            (
                "com.RoamingStar.BlueArchive",
                "com.tech.sdk.bridge.TechSDKUnityPlayerActivity",
            ),
        )

    def test_process_script_keeps_manually_assigned_category_when_saved(self):
        process_document = {
            "version": 2,
            "script_type": "module_process",
            "steps": [{"action": "home"}],
        }
        with patch.object(server_main.store, "get_script", return_value={
            "category_package": "com.RoamingStar.BlueArchive",
            "source_package": "com.RoamingStar.BlueArchive",
            "source_activity": ".MainActivity",
        }):
            metadata = server_main.script_save_metadata(
                "agent-1",
                "过程.json",
                process_document,
            )

        self.assertEqual(metadata[0], "com.RoamingStar.BlueArchive")


class ScriptManagementApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tempdir.name) / "script-api.db"))
        self.store.upsert_agent({
            "id": "agent-1",
            "name": "RK3576",
            "os": "linux",
        })

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def content():
        return json.dumps({
            "version": 2,
            "script_type": "standard",
            "steps": [
                {"action": "start"},
                {
                    "action": "launch_app",
                    "package": "com.RoamingStar.BlueArchive",
                    "activity": ".MainActivity",
                },
            ],
        }, ensure_ascii=False)

    def test_save_import_rename_move_download_and_delete(self):
        with patch.object(server_main, "store", self.store):
            saved = server_main.save_script(
                "agent-1",
                "国服.json",
                ScriptPayload(content=self.content()),
            )
            renamed = server_main.rename_script_category(
                "agent-1",
                "com.RoamingStar.BlueArchive",
                ScriptCategoryUpdate(display_name="蔚蓝档案（国服）"),
            )
            imported = server_main.import_script(
                "agent-1",
                ScriptImportPayload(
                    name="上传脚本.json",
                    content=self.content(),
                    category_package=None,
                ),
            )
            moved = server_main.move_script_category(
                "agent-1",
                "上传脚本.json",
                ScriptCategoryMove(category_package="com.RoamingStar.BlueArchive"),
            )
            response = server_main.download_script("agent-1", "上传脚本.json")
            deleted = server_main.delete_script("agent-1", "上传脚本.json")

        self.assertEqual(saved["category_package"], "com.RoamingStar.BlueArchive")
        self.assertEqual(renamed["display_name"], "蔚蓝档案（国服）")
        self.assertIsNone(imported["category_package"])
        self.assertEqual(moved["category_name"], "蔚蓝档案（国服）")
        self.assertEqual(response.body.decode("utf-8"), self.content())
        self.assertTrue(deleted["ok"])


if __name__ == "__main__":
    unittest.main()

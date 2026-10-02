from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

TOOL_PATH = Path(__file__).parents[1] / "tools" / "script_archive.py"
SPEC = importlib.util.spec_from_file_location("script_archive", TOOL_PATH)
assert SPEC and SPEC.loader
script_archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script_archive)


def data_url(payload: bytes = b"same-image") -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class ScriptArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "legacy.db"
        self.archive = self.root / "scripts.zip"
        self.create_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_database(self, *, invalid_content: str | None = None) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE scripts (
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category_package TEXT,
                    source_package TEXT,
                    source_activity TEXT
                );
                CREATE TABLE script_categories (
                    agent_id TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    display_name TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO script_categories VALUES(?,?,?)",
                (script_archive.GLOBAL_SCRIPT_SCOPE, "com.example.game", "示例游戏"),
            )
            first = {
                "version": 1,
                "script_type": "module_start",
                "steps": [
                    {"action": "start"},
                    {"action": "wait_click", "template_base64": data_url()},
                ],
            }
            second = {
                "version": 1,
                "script_type": "module_process",
                "steps": [
                    {
                        "action": "wait_click",
                        "template_base64": data_url(),
                        "image_branches": [{"template_base64": data_url(b"branch-image")}],
                    }
                ],
            }
            connection.execute(
                "INSERT INTO scripts VALUES(?,?,?,?,?,?)",
                (
                    script_archive.GLOBAL_SCRIPT_SCOPE,
                    "../开始/脚本",
                    invalid_content or json.dumps(first, ensure_ascii=False),
                    "com.example.game",
                    "com.example.game",
                    ".MainActivity",
                ),
            )
            connection.commit()
            connection.execute(
                "INSERT INTO scripts VALUES(?,?,?,?,?,?)",
                (
                    script_archive.GLOBAL_SCRIPT_SCOPE,
                    "过程脚本",
                    json.dumps(second, ensure_ascii=False),
                    "com.example.game",
                    "com.example.game",
                    ".MainActivity",
                ),
            )
            connection.commit()

    def rewrite_archive(self, transform) -> Path:
        rewritten = self.root / "rewritten.zip"
        with zipfile.ZipFile(self.archive, "r") as source, zipfile.ZipFile(rewritten, "w") as target:
            for info in source.infolist():
                name, content = transform(info.filename, source.read(info))
                target.writestr(name, content)
        return rewritten

    def test_exports_unicode_and_deduplicates_resources(self) -> None:
        result = script_archive.export_archive(self.database, self.archive)
        self.assertEqual(result["statistics"]["scripts"], 2)
        self.assertEqual(result["statistics"]["categories"], 1)
        self.assertEqual(result["statistics"]["resource_references"], 3)
        self.assertEqual(result["statistics"]["unique_resources"], 2)
        self.assertIn("../开始/脚本", [item["name"] for item in result["scripts"]])
        with zipfile.ZipFile(self.archive) as archive:
            script_paths = sorted(name for name in archive.namelist() if name.startswith("scripts/"))
            self.assertEqual(script_paths, ["scripts/0001.json", "scripts/0002.json"])

    def test_repeated_export_has_same_logical_digest(self) -> None:
        first = script_archive.export_archive(self.database, self.archive)
        second_path = self.root / "second.zip"
        second = script_archive.export_archive(self.database, second_path)
        self.assertEqual(first["logical_sha256"], second["logical_sha256"])

    def test_detects_tampered_script(self) -> None:
        script_archive.export_archive(self.database, self.archive)

        def transform(name: str, content: bytes) -> tuple[str, bytes]:
            if name == "scripts/0001.json":
                return name, content.replace(b'"version":1', b'"version":2')
            return name, content

        with self.assertRaises(script_archive.ArchiveError) as captured:
            script_archive.validate_archive(self.rewrite_archive(transform))
        self.assertEqual(captured.exception.code, "hash_mismatch")

    def test_rejects_path_traversal_entry(self) -> None:
        script_archive.export_archive(self.database, self.archive)
        injected = self.root / "injected.zip"
        with zipfile.ZipFile(self.archive, "r") as source, zipfile.ZipFile(injected, "w") as target:
            for info in source.infolist():
                target.writestr(info.filename, source.read(info))
            target.writestr("../escape.txt", b"no")
        with self.assertRaises(script_archive.ArchiveError) as captured:
            script_archive.validate_archive(injected)
        self.assertEqual(captured.exception.code, "unsafe_archive_path")

    def test_invalid_json_does_not_replace_existing_archive(self) -> None:
        self.database.unlink()
        self.create_database(invalid_content="not-json")
        self.archive.write_bytes(b"known-good-placeholder")
        with self.assertRaises(script_archive.ArchiveError) as captured:
            script_archive.export_archive(self.database, self.archive)
        self.assertEqual(captured.exception.code, "invalid_script_json")
        self.assertEqual(self.archive.read_bytes(), b"known-good-placeholder")

    def test_rejects_invalid_data_url(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            document = {
                "version": 1,
                "script_type": "module_process",
                "steps": [{"action": "wait_click", "template_base64": "data:image/png;base64,%%%"}],
            }
            connection.execute(
                "UPDATE scripts SET content=? WHERE name=?",
                (json.dumps(document), "过程脚本"),
            )
            connection.commit()
        with self.assertRaises(script_archive.ArchiveError) as captured:
            script_archive.export_archive(self.database, self.archive)
        self.assertEqual(captured.exception.code, "invalid_base64")


if __name__ == "__main__":
    unittest.main()

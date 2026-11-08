# -*- coding: utf-8 -*-
"""Synthetic coverage for the T3 entry adapter and native-parser reuse."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import t3code
import scan_t3
import session_review


THREAD_ID = "synthetic-thread-0001"
CLAUDE_ID = "synthetic-claude-session"
CODEX_ID = "synthetic-codex-session"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def make_fixture(home):
    db = home / ".t3" / "userdata" / "statev2.sqlite"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE projection_projects (
            project_id TEXT PRIMARY KEY, title TEXT, workspace_root TEXT,
            scripts_json TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE orchestration_v2_projection_threads (
            thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            default_provider TEXT, runtime_mode TEXT, interaction_mode TEXT,
            active_provider_thread_id TEXT, created_at TEXT, updated_at TEXT,
            archived_at TEXT, deleted_at TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_runs (
            run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER,
            provider TEXT, provider_thread_id TEXT, status TEXT,
            requested_at TEXT, completed_at TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_provider_threads (
            provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, owner_node_id TEXT,
            provider TEXT, provider_session_id TEXT, status TEXT,
            first_run_ordinal INTEGER, last_run_ordinal INTEGER, updated_at TEXT,
            payload_json TEXT, driver TEXT, provider_instance_id TEXT
        );
        CREATE TABLE orchestration_v2_projection_provider_sessions (
            provider_session_id TEXT, thread_id TEXT, provider TEXT, status TEXT,
            model TEXT, updated_at TEXT, payload_json TEXT, driver TEXT,
            provider_instance_id TEXT
        );
        CREATE TABLE orchestration_v2_projection_turn_items (
            turn_item_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, node_id TEXT,
            provider_thread_id TEXT, provider_turn_id TEXT, parent_item_id TEXT,
            ordinal INTEGER, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_context_handoffs (
            context_handoff_id TEXT PRIMARY KEY, thread_id TEXT, target_run_id TEXT,
            to_provider_thread_id TEXT, strategy TEXT, status TEXT, updated_at TEXT,
            payload_json TEXT
        );
    """)
    c.execute("INSERT INTO projection_projects VALUES(?,?,?,?,?,?)",
              ("project-synthetic", "【合成样例】项目", "C:/synthetic/project", "[]",
               "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    c.execute("INSERT INTO orchestration_v2_projection_threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (THREAD_ID, "project-synthetic", "【合成样例】T3 聚合",
               "magpie", "read-only", "default", "provider-codex",
               "2026-01-01T00:00:00Z", "2026-01-01T00:04:00Z", None, None, "{}"))
    for ordinal, provider, thread_ref in [
        (1, "magpie", "provider-claude"),
        (2, "codex", "provider-codex"),
    ]:
        run_id = "run-" + str(ordinal)
        c.execute("INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?,?,?,?,?,?)",
                  (run_id, THREAD_ID, ordinal, provider, thread_ref, "completed",
                   "2026-01-01T00:00:00Z", "2026-01-01T00:04:00Z", "{}"))
    provider_rows = [
        ("provider-claude", "magpie", "claudeAgent", "magpie", "session-claude",
         {"driver": "claudeAgent", "nativeId": CLAUDE_ID, "strength": "strong"}),
        ("provider-codex", "codex", "codex", "codex", "session-codex",
         {"driver": "codex", "nativeId": CODEX_ID, "strength": "strong"}),
    ]
    for thread_ref, provider, driver, instance, session_id, native in provider_rows:
        payload = json.dumps({"nativeThreadRef": native}, ensure_ascii=False)
        c.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (thread_ref, THREAD_ID, None, provider, session_id,
                   "idle", 1, 2, "2026-01-01T00:04:00Z", payload, driver, instance))
        model = None if provider == "magpie" else "synthetic-model-codex"
        session_payload = json.dumps({"model": "synthetic-model-magpie"}) if provider == "magpie" else "{}"
        c.execute("INSERT INTO orchestration_v2_projection_provider_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                  (session_id, THREAD_ID, provider, "stopped",
                   model, "2026-01-01T00:04:00Z", session_payload, driver, instance))
    c.execute("INSERT INTO orchestration_v2_projection_provider_sessions VALUES(?,?,?,?,?,?,?,?,?)",
              ("session-codex", "another-synthetic-thread", "wrong", "stopped",
               "wrong-model", "2026-01-01T00:04:00Z", "{}", "wrong", "wrong"))
    messages = [
        (1, "user_message", "user", "【合成样例】请检查这个项目。"),
        (2, "assistant_message", "assistant", "【合成样例】我先核对 Harness 来源。"),
        (3, "user_message", "user", "【合成样例】再给出本地入口。"),
        (4, "assistant_message", "assistant",
         "[打开合成报告](</C:/synthetic/project/index.html>)\n\n最终结论已经核实。"),
    ]
    for ordinal, item_type, role, text in messages:
        payload = json.dumps({"text": text, "role": role}, ensure_ascii=False)
        c.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("item-" + str(ordinal), THREAD_ID, "run-2", "node-" + str(ordinal),
                   "provider-codex", "turn-synthetic", None, ordinal, item_type,
                   "completed", "2026-01-01T00:0%d:00Z" % ordinal, payload))
    secretish = json.dumps({"command": "echo synthetic", "output": "synthetic-tool-output"})
    c.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("tool-item", THREAD_ID, "run-2", "node-tool", "provider-codex",
               "turn-synthetic", None, 5, "command_execution", "completed",
               "2026-01-01T00:05:00Z", secretish))
    c.commit()
    c.close()

    write_jsonl(home / ".claude" / "projects" / "fixture" / (CLAUDE_ID + ".jsonl"), [
        {"type": "user", "sessionId": CLAUDE_ID, "uuid": "u1", "cwd": "C:/synthetic/project",
         "message": {"role": "user", "content": "【合成样例】先由 Claude 处理。"},
         "timestamp": "2026-01-01T00:01:00Z"},
        {"type": "assistant", "sessionId": CLAUDE_ID, "uuid": "a1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "【合成样例】Claude 输出。"}]},
         "timestamp": "2026-01-01T00:02:00Z"},
    ])
    write_jsonl(home / ".codex" / "sessions" / "2026" / "01" / "01" /
                ("rollout-2026-01-01T00-03-00-" + CODEX_ID + ".jsonl"), [
        {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "C:/synthetic/project"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:04:00Z",
         "payload": {"type": "task_complete", "last_agent_message": "【合成样例】Codex 输出。"}},
    ])
    return db


class TestT3Identity(unittest.TestCase):
    def test_context_uri_and_prefixed_id_resolve_exactly(self):
        uri = "t3-context://v1/thread/thread_" + THREAD_ID
        self.assertEqual(t3code.normalize_thread_ref(uri)[0], THREAD_ID)
        self.assertEqual(t3code.normalize_thread_ref("thread_" + THREAD_ID)[0], THREAD_ID)

    def test_last_sentence_ignores_inline_file_link(self):
        self.assertEqual(
            t3code._last_sentence("[file](</tmp/report.html>) Conclusion is verified."),
            "Conclusion is verified.",
        )

    def test_last_sentence_keeps_link_label_when_part_of_sentence(self):
        self.assertEqual(
            t3code._last_sentence("Open [source](</tmp/source.html>)."),
            "Open source.",
        )

    def test_context_uri_round_trips_opaque_internal_id(self):
        ident = "thread:delegated-task:synthetic"
        uri = t3code.normalize_thread_ref(ident)[1]
        self.assertEqual(t3code.normalize_thread_ref(uri)[0], ident)

    def test_opaque_t3_ids_are_supported(self):
        ident = "thread:delegated-task:synthetic"
        self.assertEqual(t3code.normalize_thread_ref(ident)[0], ident)
        self.assertIn("thread%3Adelegated-task", t3code.normalize_thread_ref(ident)[1])


class TestT3Extraction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = make_fixture(self.home)
        self.uri = "t3-context://v1/thread/thread_" + THREAD_ID

    def tearDown(self):
        self.temp.cleanup()

    def test_projection_and_native_harness_parsers_are_joined(self):
        product = t3code.extract_thread(self.uri, home=self.home)
        self.assertEqual(product["thread"]["thread_id"], THREAD_ID)
        self.assertEqual([x["driver"] for x in product["native_harness_sessions"]],
                         ["claudeAgent", "codex"])
        codex_context = product["native_harness_sessions"][1]["provider_contexts"][0]
        self.assertEqual(codex_context["provider_model"], "synthetic-model-codex")
        claude_context = product["native_harness_sessions"][0]["provider_contexts"][0]
        self.assertEqual(claude_context["provider_model"], "synthetic-model-magpie")
        self.assertTrue(all(x["status"] == "parsed"
                            for x in product["native_harness_sessions"]))
        codex = next(x for x in product["native_harness_sessions"]
                     if x["platform"] == "codex")
        self.assertEqual(codex["content_coverage"], "task-complete-only")
        self.assertEqual(codex["regular_user_message_count"], 0)
        self.assertEqual(codex["task_complete_summary_count"], 1)
        self.assertEqual(product["latest_assistant"]["file_links"],
                         ["/C:/synthetic/project/index.html"])
        self.assertEqual(product["latest_assistant"]["last_sentence"],
                         "最终结论已经核实。")
        self.assertNotIn("synthetic-tool-output",
                         json.dumps(product, ensure_ascii=False))
        summary = t3code.summary_text(product)
        self.assertIn("driver=claudeAgent parser=claude provider=magpie", summary)
        self.assertIn("driver=codex parser=codex provider=codex", summary)

    def test_missing_harness_keeps_projected_messages(self):
        (self.home / ".codex").rename(self.home / ".codex-offline")
        product = t3code.extract_thread(self.uri, home=self.home)
        self.assertEqual(product["messages"][-1]["role"], "assistant")
        self.assertIn("not_found", [x["status"] for x in product["native_harness_sessions"]])

    def test_cli_reads_t3_without_custom_extraction_code(self):
        cli = SCRIPTS / "session_inspect.py"
        result = subprocess.run([
            sys.executable, "-I", str(cli), "read", "--platform", "t3",
            "--session", self.uri, "--home", str(self.home), "--json",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        product = json.loads(result.stdout)
        self.assertEqual(product["latest_assistant"]["last_sentence"],
                         "最终结论已经核实。")
        self.assertEqual(product["_meta"]["coverage"]["native_harness_sessions_found"], 2)

    def test_out_writes_full_product_and_keeps_stdout_compact(self):
        cli = SCRIPTS / "session_inspect.py"
        out = self.home / "extract.json"
        result = subprocess.run([
            sys.executable, "-I", str(cli), "read", "--platform", "t3",
            "--session", self.uri, "--home", str(self.home), "--json",
            "--out", str(out),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.is_file())
        product = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(product["latest_assistant"]["last_sentence"],
                         "最终结论已经核实。")
        self.assertIn("LATEST ASSISTANT OUTPUT", result.stdout)
        self.assertNotIn('"turn_items"', result.stdout)

    def test_t3_overview_does_not_offer_native_resume(self):
        records = scan_t3.scan(days=0, session_id=self.uri, home=self.home)
        session_review.annotate_action_fields(records)
        self.assertEqual(len(records), 1)
        self.assertIn("read --platform t3", records[0]["next_action_hint"])
        self.assertEqual(session_review.select_action_records(records, 10), [])

    def test_database_reader_is_read_only(self):
        store = t3code.T3Store(db_path=self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.connection.execute(
                    "INSERT INTO orchestration_v2_projection_threads "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("forbidden", "project-synthetic", "forbidden", "codex",
                     "read-only", "default", "", "", "", None, None, "{}"))
        finally:
            store.close()

    def test_cli_help_advertises_t3_entry(self):
        cli = SCRIPTS / "session_inspect.py"
        result = subprocess.run(
            [sys.executable, "-I", str(cli), "overview", "--help"],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("t3", result.stdout.lower())

    def test_overview_supports_exact_t3_uri_and_search(self):
        cli = SCRIPTS / "session_inspect.py"
        result = subprocess.run([
            sys.executable, "-I", str(cli), "overview", "--platform", "t3",
            "--session", self.uri, "--home", str(self.home), "--json",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["context_uri"], self.uri)


if __name__ == "__main__":
    unittest.main()

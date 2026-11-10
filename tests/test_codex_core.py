"""Contracts for the shared Codex core and native handoff/read CLI behavior.

All fixtures are synthetic and written to a temporary directory. No real user
history, no T3 database and no network access is involved.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_core  # noqa: E402
import extract_codex  # noqa: E402
import scan_codex  # noqa: E402


SID = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def meta(sid=SID, **extra):
    return {"type": "session_meta",
            "payload": {"id": sid, "cwd": "/synthetic/project", **extra}}


def event(text, role="user", ts="2026-11-01T00:00:00Z"):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": role + "_message", "message": text}}


def response(text, role="user", ts="2026-11-01T00:00:00Z"):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "message", "role": role,
                        "content": [{
                            "type": "input_text" if role == "user" else "output_text",
                            "text": text}]}}


def write(home, name, rows):
    path = Path(home) / ".codex" / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")
    return path


class CodexCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_response_item_body_is_read_by_both_views(self):
        path = write(self.home, "new.jsonl", [
            meta(), response("user body"), response("agent body", "assistant")])
        parsed = scan_codex.parse_codex_file(path)
        self.assertIn("user body", parsed["user_messages"])
        result = extract_codex.extract(path)
        self.assertIn("user body", [m["text"] for m in result["user_msgs"]])
        self.assertIn("agent body", [m["text"] for m in result["agent_texts"]])

    def test_mirrored_message_is_counted_once_and_declared(self):
        path = write(self.home, "mirror.jsonl", [
            meta(), event("same"), response("same", ts="2026-11-01T00:00:01Z")])
        source = codex_core.parse_source(path)
        self.assertEqual(source["mirrored_response_items"], 1)
        self.assertEqual(
            sum(1 for e in source["events"] if e["role"] == "user"), 1)

    def test_two_real_identical_messages_stay_two_events(self):
        path = write(self.home, "repeat.jsonl", [
            meta(), event("same", ts="2026-11-01T00:00:00Z"),
            event("same", ts="2026-11-01T00:00:01Z")])
        source = codex_core.parse_source(path)
        self.assertEqual(
            sum(1 for e in source["events"] if e["role"] == "user"), 2)

    def test_full_body_is_not_truncated(self):
        text = "  " + ("a" * 2500) + "TAIL_MARKER\n"
        path = write(self.home, "long.jsonl", [meta(), event(text)])
        self.assertEqual(extract_codex.extract(path)["user_msgs"][0]["text"], text)

    def test_task_complete_is_not_a_regular_agent_message(self):
        done = {"type": "event_msg", "timestamp": "2026-11-01T00:00:02Z",
                "payload": {"type": "task_complete", "last_agent_message": "done"}}
        path = write(self.home, "tc.jsonl", [meta(), done])
        result = extract_codex.extract(path)
        self.assertEqual(result["total_agent_msgs"], 0)
        self.assertTrue(result["agent_texts"][0]["is_task_complete"])

    def test_incomplete_tail_is_reported_and_prefix_survives(self):
        path = write(self.home, "partial.jsonl", [meta(), event("complete prefix")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":')
        result = extract_codex.extract(path)
        self.assertEqual(result["user_msgs"][0]["text"], "complete prefix")
        self.assertTrue(result["coverage"]["incomplete_tail"])

    def test_compressed_source_is_discovered_even_without_decoder(self):
        path = write(self.home, "ordinary.jsonl", [meta()])
        compressed = path.with_suffix(".jsonl.zst")
        compressed.write_bytes(b"fixture-not-decoded")
        discovered = list(scan_codex.iter_codex_files(self.home))
        self.assertIn(compressed, discovered)
        source = codex_core.parse_source(compressed)
        self.assertFalse(source["readable"])
        self.assertEqual(
            source["coverage"]["compressed_sources"][0]["readable"], False)

    def test_unreadable_compressed_source_is_not_reported_as_empty(self):
        path = write(self.home, "ordinary.jsonl", [meta()])
        path.with_suffix(".jsonl.zst").write_bytes(b"fixture-not-decoded")
        compressed = path.with_suffix(".jsonl.zst")
        # The source stays visible, but is marked unreadable rather than being
        # counted as a real zero-message session.
        summary = scan_codex.parse_codex_file(compressed)
        self.assertIsNotNone(summary)
        self.assertFalse(summary["readable"])
        self.assertEqual(summary["coverage"]["body_read"], "unreadable")

    def test_read_reports_why_compressed_source_is_unreadable(self):
        compressed = (Path(self.home) / ".codex" / "sessions"
                      / "rollout-{}.jsonl.zst".format(SID))
        compressed.parent.mkdir(parents=True, exist_ok=True)
        compressed.write_bytes(b"fixture-not-decoded")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "session_inspect.py"), "read",
             "--platform", "codex", str(compressed), "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertNotEqual(result.returncode, 0)
        output = (result.stdout + result.stderr).lower()
        self.assertIn("无法解码", output)

    def test_bom_prefixed_jsonl_is_readable(self):
        path = Path(self.home) / ".codex" / "sessions" / "bom.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\ufeff" + json.dumps(meta()) + "\n" + json.dumps(event("body")) + "\n",
            encoding="utf-8")
        self.assertEqual(extract_codex.extract(path)["user_msgs"][0]["text"], "body")


class NativeCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.source = write(
            self.home, "rollout-{}.jsonl".format(SID),
            [meta(), response("first body")])

    def call(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "session_inspect.py"), *map(str, args)],
            cwd=self.home, capture_output=True, text=True,
            encoding="utf-8", timeout=20)

    def test_read_out_writes_same_product_it_prints(self):
        target = self.home / "read.json"
        result = self.call("read", "--session", SID, "--home", self.home,
                           "--json", "--out", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(printed, saved)

    def test_handoff_receipt_reports_hash_and_scope(self):
        target = self.home / "handoff.json"
        result = self.call("handoff", "--session", SID, "--home", self.home,
                           "--out", target, "--verify", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["wrote"], str(target.resolve()))
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertEqual(receipt["verification"]["ok"], True)

    def test_handoff_openai_variant_is_written(self):
        target = self.home / "handoff.json"
        result = self.call("handoff", "--session", SID, "--home", self.home,
                           "--out", target, "--as", "openai")
        self.assertEqual(result.returncode, 0, result.stderr)
        variant = self.home / "handoff.openai.json"
        self.assertTrue(variant.is_file())
        product = json.loads(variant.read_text(encoding="utf-8"))
        self.assertEqual(product["messages"][0]["role"], "user")

    def test_handoff_never_clobbers_previous_product_on_error(self):
        target = self.home / "handoff.json"
        target.write_text('{"sentinel": true}', encoding="utf-8")
        result = self.call("handoff", "--session", "deadbeef-0000-4000-8000-000000000000",
                           "--home", self.home, "--out", target)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"sentinel": True})

    def test_stdout_stays_json_with_large_file_note(self):
        result = self.call("read", "--session", SID, "--home", self.home, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()

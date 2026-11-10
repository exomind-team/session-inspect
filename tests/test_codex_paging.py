"""Contracts for strict literal search and budgeted paging (M3)."""
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


SID = "11111111-2222-4333-8444-555555555555"


def meta():
    return {"type": "session_meta",
            "payload": {"id": SID, "cwd": "/synthetic/project"}}


def event(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "user_message", "message": text}}


def write(home, rows):
    path = Path(home) / ".codex" / "sessions" / "rollout-{}.jsonl".format(SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")
    return path


class LiteralSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_finds_early_middle_and_long_tail_matches(self):
        long_text = ("前" * 2500) + "不做计费"
        rows = [meta(), event("不做计费 first", "2026-11-01T00:00:00Z")]
        rows += [event("filler %d" % i, "2026-11-01T00:%02d:00Z" % (i + 1))
                 for i in range(40)]
        rows.append(event(long_text, "2026-11-01T01:00:00Z"))
        path = write(self.home, rows)
        result = extract_codex.extract(path)
        hits = codex_core.find_literal(result["sources"][0]["events"], "不做计费")
        self.assertEqual(len(hits), 2)
        self.assertEqual([h["role"] for h in hits], ["user", "user"])

    def test_two_char_chinese_term_is_matched_literally(self):
        path = write(self.home, [meta(), event("这里有一个计费问题",
                                               "2026-11-01T00:00:00Z")])
        result = extract_codex.extract(path)
        hits = codex_core.find_literal(result["sources"][0]["events"], "计费")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["match"], "计费")

    def test_uuid_and_code_identifier_are_matched(self):
        text = "see 11111111-2222-4333-8444-555555555555 and scan_codex.py"
        path = write(self.home, [meta(), event(text, "2026-11-01T00:00:00Z")])
        result = extract_codex.extract(path)
        events = result["sources"][0]["events"]
        self.assertEqual(len(codex_core.find_literal(
            events, "11111111-2222-4333-8444-555555555555")), 1)
        self.assertEqual(len(codex_core.find_literal(events, "scan_codex.py")), 1)

    def test_case_sensitivity_is_explicit(self):
        path = write(self.home, [meta(), event("TailMarker", "2026-11-01T00:00:00Z")])
        events = extract_codex.extract(path)["sources"][0]["events"]
        self.assertEqual(len(codex_core.find_literal(events, "tailmarker")), 1)
        self.assertEqual(len(codex_core.find_literal(
            events, "tailmarker", case_sensitive=True)), 0)


class Paging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.rows = [meta()]
        for i in range(5):
            self.rows.append(event("消息%d" % i + ("x" * 40),
                                   "2026-11-01T00:0%d:00Z" % i))
        self.path = write(self.home, self.rows)
        self.result = extract_codex.extract(self.path)
        self.sources = self.result["sources"]
        self.events = self.sources[0]["events"]

    def test_budget_is_respected(self):
        page = codex_core.paginate_events(
            self.events, sources=self.sources, budget_chars=50)
        self.assertLessEqual(page["budget"]["delivered"], 50)
        self.assertEqual(page["budget"]["unit"], "chars")

    def test_same_cursor_returns_same_page(self):
        first = codex_core.paginate_events(
            self.events, sources=self.sources, budget_chars=50)
        again = codex_core.paginate_events(
            self.events, sources=self.sources,
            cursor=first["next_cursor"], budget_chars=50)
        retry = codex_core.paginate_events(
            self.events, sources=self.sources,
            cursor=first["next_cursor"], budget_chars=50)
        self.assertEqual(again, retry)

    def test_consecutive_pages_do_not_overlap_or_skip(self):
        cursor = None
        rebuilt = []
        pages = 0
        while True:
            page = codex_core.paginate_events(
                self.events, sources=self.sources,
                cursor=cursor, budget_chars=37)
            pages += 1
            for item in page["items"]:
                rebuilt.append(item["text"])
            cursor = page["next_cursor"]
            if not cursor:
                break
            self.assertLess(pages, 100)
        self.assertEqual("".join(rebuilt), "".join(e["text"] for e in self.events))

    def test_source_change_is_detected(self):
        page = codex_core.paginate_events(
            self.events, sources=self.sources, budget_chars=30)
        changed = [dict(self.sources[0], sha256="0" * 64)]
        with self.assertRaises(codex_core.CursorError) as ctx:
            codex_core.paginate_events(
                self.events, sources=changed,
                cursor=page["next_cursor"], budget_chars=30)
        self.assertEqual(ctx.exception.code, "SOURCE_CHANGED")

    def test_invalid_cursor_is_rejected(self):
        with self.assertRaises(codex_core.CursorError):
            codex_core.paginate_events(
                self.events, sources=self.sources,
                cursor="not-a-cursor", budget_chars=30)

    def test_cursor_cannot_carry_paths(self):
        payload = {
            "schema_version": 1,
            "adapter_version": codex_core.ADAPTER_VERSION,
            "snapshot": codex_core.snapshot_id(self.sources),
            "selection": "messages",
            "event_index": 0,
            "char_offset": 0,
        }
        payload["path"] = "/etc/passwd"
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        import base64

        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        with self.assertRaises(codex_core.CursorError) as ctx:
            codex_core.paginate_events(
                self.events, sources=self.sources, cursor=token, budget_chars=30)
        self.assertEqual(ctx.exception.code, "INVALID_CURSOR")

    def test_selection_mismatch_is_detected(self):
        page = codex_core.paginate_events(
            self.events, sources=self.sources,
            budget_chars=30, selection="messages")
        with self.assertRaises(codex_core.CursorError) as ctx:
            codex_core.paginate_events(
                self.events, sources=self.sources,
                cursor=page["next_cursor"], budget_chars=30, selection="other")
        self.assertEqual(ctx.exception.code, "CURSOR_SELECTION_MISMATCH")


class PagingCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        rows = [meta()]
        for i in range(4):
            rows.append(event("消息%d " % i + "y" * 30,
                              "2026-11-01T00:0%d:00Z" % i))
        rows.append(event("tail 不做计费", "2026-11-01T01:00:00Z"))
        self.source = write(self.home, rows)

    def call(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "session_inspect.py"), *map(str, args)],
            cwd=self.home, capture_output=True, text=True,
            encoding="utf-8", timeout=20)

    def test_find_literal_json_stdout_is_pure_json(self):
        result = self.call("find", "--session", SID, "--home", self.home,
                           "--literal", "不做计费", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["hit_count"], 1)
        self.assertIn("Found", result.stderr)

    def test_find_literal_without_hit_is_not_an_error(self):
        result = self.call("find", "--session", SID, "--home", self.home,
                           "--literal", "不存在的词", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hit_count"], 0)

    def test_find_literal_requires_a_source(self):
        result = self.call("find", "--literal", "x", "--json")
        self.assertNotEqual(result.returncode, 0)

    def test_read_budget_pages_cover_all_text(self):
        cursor = None
        texts = []
        for _ in range(50):
            args = ["read", "--session", SID, "--home", self.home,
                    "--budget", "40", "--json"]
            if cursor:
                args += ["--cursor", cursor]
            result = self.call(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            page = json.loads(result.stdout)
            texts.extend(item["text"] for item in page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        self.assertIsNone(cursor)
        self.assertIn("不做计费", "".join(texts))
        self.assertEqual(len("".join(texts)),
                         sum(len(e["text"]) for e in
                             extract_codex.extract(self.source)["sources"][0]["events"]))

    def test_changed_source_rejects_old_cursor(self):
        first = self.call("read", "--session", SID, "--home", self.home,
                          "--budget", "40", "--json")
        cursor = json.loads(first.stdout)["next_cursor"]
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event("appended", "2026-11-01T02:00:00Z"),
                                    ensure_ascii=False) + "\n")
        result = self.call("read", "--session", SID, "--home", self.home,
                           "--budget", "40", "--cursor", cursor, "--json")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "SOURCE_CHANGED")


if __name__ == "__main__":
    unittest.main()

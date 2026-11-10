"""Synthetic contracts for ChatGPT web capture normalization.

No browser, network or real conversation is used here: the snapshot fixtures
mirror the DOM shape verified manually (turn key -> role heading -> markdown),
so the normalizer's guarantees are testable offline.
"""
import base64
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chatgpt_core  # noqa: E402
import chatgpt_web  # noqa: E402


def snapshot(**overrides):
    data = {
        "url": "https://chatgpt.com/s/t_synthetic",
        "title": "ChatGPT - 共享内容",
        "captured_at": "2026-11-10T00:00:00Z",
        "messages": [
            {"role": "assistant", "text": "第一段\n\n第二段\n",
             "id": "m-1", "turn": "t-1"},
        ],
        "attachments": [],
        "images": [],
    }
    data.update(overrides)
    return data


class SnapshotNormalization(unittest.TestCase):
    def test_text_is_preserved_exactly(self):
        text = "  前导空白\n\n## 标题\n\n结尾  "
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(messages=[{"role": "assistant", "text": text, "id": "m"}],
                     attachments=[], images=[]))
        self.assertEqual(normalized["messages"][0]["text"], text)

    def test_empty_page_is_not_an_empty_success(self):
        with self.assertRaises(chatgpt_core.ChatGPTError) as ctx:
            chatgpt_core.normalize_browser_snapshot(
                snapshot(messages=[], attachments=[], images=[]))
        self.assertEqual(ctx.exception.code, "EMPTY_CONVERSATION")

    def test_whitespace_only_is_rejected(self):
        with self.assertRaises(chatgpt_core.ChatGPTError) as ctx:
            chatgpt_core.normalize_browser_snapshot(
                snapshot(messages=[{"role": "assistant", "text": "   "}]))
        self.assertEqual(ctx.exception.code, "EMPTY_TEXT")

    def test_unknown_role_is_reported_not_guessed(self):
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(messages=[{"role": "assistant", "text": "a"},
                               {"role": "", "text": "b"}]))
        self.assertEqual(normalized["coverage"]["unknown_role_count"], 1)
        self.assertEqual(normalized["messages"][1]["role"], "unknown")

    def test_role_user_is_not_claimed_as_human(self):
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(messages=[{"role": "user", "text": "hi"}]))
        self.assertNotIn("human", str(normalized["coverage"]).lower())

    def test_attachment_bytes_are_counted_only_when_fetched(self):
        attachments = [
            {"name": "a.md", "downloadable": True, "bytes_available": True,
             "bytes": 10, "sha256": "0" * 64},
            {"name": "b.md", "downloadable": True, "bytes_available": False},
        ]
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(attachments=attachments))
        self.assertEqual(normalized["coverage"]["attachments_listed"], 2)
        self.assertEqual(normalized["coverage"]["attachments_fetched"], 1)
        self.assertFalse(normalized["coverage"]["complete"])

    def test_repeated_identical_messages_are_both_kept(self):
        text = "继续"
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(messages=[
                {"role": "user", "text": text, "id": "a"},
                {"role": "user", "text": text, "id": "b"}]))
        self.assertEqual(len(normalized["messages"]), 2)


class ProductEnvelope(unittest.TestCase):
    def test_product_is_rescued_session_shaped(self):
        normalized = chatgpt_core.normalize_browser_snapshot(snapshot())
        product = chatgpt_core.build_product(normalized, title="标题")
        self.assertEqual(product["_meta"]["schema"], "rescued-session/v1")
        self.assertEqual(product["conversations"][0]["title"], "标题")
        self.assertEqual(
            product["conversations"][0]["transcript"][0]["type"], "RESPONSE")
        self.assertTrue(product["_meta"]["verification"]["ok"])

    def test_verification_does_not_claim_source_completeness(self):
        normalized = chatgpt_core.normalize_browser_snapshot(snapshot())
        product = chatgpt_core.build_product(normalized)
        self.assertIn("does not prove",
                      product["_meta"]["verification"]["scope"].lower())

    def test_attachment_base64_only_when_requested(self):
        encoded = base64.b64encode(b"# hello").decode("ascii")
        attachments = [{"name": "a.md", "bytes_available": True,
                        "bytes": 7, "content_base64": encoded}]
        normalized = chatgpt_core.normalize_browser_snapshot(
            snapshot(attachments=attachments))
        without = chatgpt_core.build_product(normalized)
        with_bytes = chatgpt_core.build_product(normalized, include_base64=True)
        self.assertNotIn(
            "content_base64", without["conversations"][0]["attachments"][0])
        self.assertEqual(
            with_bytes["conversations"][0]["attachments"][0]["content_base64"],
            encoded)


class UrlBoundary(unittest.TestCase):
    def test_only_chatgpt_https_is_supported(self):
        self.assertTrue(chatgpt_web.is_supported_url(
            "https://chatgpt.com/s/t_abc"))
        self.assertFalse(chatgpt_web.is_supported_url(
            "http://chatgpt.com/s/t_abc"))
        self.assertFalse(chatgpt_web.is_supported_url(
            "https://evil.example/chatgpt.com/s/t_abc"))

    def test_share_url_detection(self):
        self.assertTrue(chatgpt_web.is_share_url(
            "https://chatgpt.com/s/t_abc"))
        self.assertFalse(chatgpt_web.is_share_url(
            "https://chatgpt.com/g/g-p-x/c/abc"))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""
本轮迭代新增能力的回归测试（stdlib unittest，不引第三方依赖）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_deepseek_tree.py

⚠️ **脱敏原则**：所有会话内容都是**合成样例**，不含任何真实会话正文、
真实标题、真实链接或真实 session id。

覆盖的是**本轮真实踩过的坑**，每条断言后面标注它守住的是什么：
  1. `children[0]` 走主链会在首个分叉点选中旧草稿，静默丢掉后续节点
  2. 手写章节清单覆盖不全 → 正文短一截且不报错
  3. 同名附件按 file_name 落盘 → 静默覆盖
  4. 文档类附件恒 400 → 静默失败
  5. 跨源比对的派生字段/签名漂移 → 假阳性
  6. verify-package 请求了不存在的文档却报 PASS（假绿）
"""
import json
import os
import sys
import unittest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

import deepseek_common  # noqa: E402
import deepseek_tree  # noqa: E402


def _node(nid, parent, children, content, ts="2026-01-01T00:00:00"):
    return {"id": nid, "parent": parent, "children": children,
            "message": {"inserted_at": ts,
                        "fragments": [{"type": "RESPONSE", "content": content}]}}


def _forked_tree():
    """
    复刻那次事故的树形：n6 分叉 → [7(旧草稿), 9(主链继续)]。

        root → 1 → 3 → 6 ─┬─ 7            (支线：被改写前的旧草稿)
                          └─ 9 → 11 → 13   (主链)
    """
    return {
        "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
        "1": _node("1", "root", ["3"], "A"),
        "3": _node("3", "1", ["6"], "B"),
        "6": _node("6", "3", ["7", "9"], "C"),
        "7": _node("7", "6", [], "D-old-draft"),
        "9": _node("9", "6", ["11"], "D-main"),
        "11": _node("11", "9", ["13"], "E"),
        "13": _node("13", "11", [], "F"),
    }


class MainPath(unittest.TestCase):
    def test_naive_children_first_would_drop_nodes(self):
        """守住 1：children[0] 走法确实会丢节点——这是做原语的理由。"""
        m = _forked_tree()
        walked, cur = [], m["root"]
        while cur.get("children"):
            cur = m[cur["children"][0]]
            walked.append(cur["id"])
        self.assertEqual(walked, ["1", "3", "6", "7"])
        self.assertEqual(sorted(set(m) - set(walked) - {"root"}), ["11", "13", "9"])

    def test_current_id_walks_the_real_chain(self):
        """守住 1：沿 current_id 回溯得到网页实际显示的那条链。"""
        self.assertEqual(
            deepseek_tree.main_path_ids(_forked_tree(), "13"),
            ["1", "3", "6", "9", "11", "13"])

    def test_root_sentinel_excluded(self):
        self.assertNotIn("root", deepseek_tree.main_path_ids(_forked_tree(), "13"))


class BranchIndex(unittest.TestCase):
    def test_branch_is_keyed_by_fork_not_start(self):
        """守住 1：支线挂在**分叉点**上，同分叉点可挂多条。"""
        m = _forked_tree()
        branches = deepseek_tree.branch_index(m, None, "13")
        self.assertEqual(len(branches), 1)
        fork, nodes = branches[0]
        self.assertEqual(fork, "6")
        self.assertEqual(nodes, ["7"])

    def test_multiple_branches_share_one_fork(self):
        """同一分叉点挂多条支线时，用起点节点区分（不能只记分叉点）。"""
        m = _forked_tree()
        m["6"]["children"] = ["7", "8", "9"]
        m["8"] = _node("8", "6", [], "D-other-draft")
        branches = deepseek_tree.branch_index(m, None, "13")
        self.assertEqual([n[0] for _f, n in branches], ["7", "8"])
        self.assertEqual({f for f, _n in branches}, {"6"})

    def test_coverage_is_exact(self):
        """守住 2：主链 + 支线必须恰好覆盖全部节点。"""
        m = _forked_tree()
        main = deepseek_tree.main_path_ids(m, "13")
        branches = deepseek_tree.branch_index(m, main, "13")
        covered, all_nodes, missing, extra = deepseek_tree.coverage(m, main, branches)
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])
        self.assertEqual(covered, all_nodes)


class RenderCoverage(unittest.TestCase):
    def test_renders_both_lines(self):
        md = deepseek_tree.render_markdown(_forked_tree(), "13", title="T")
        self.assertIn("D-old-draft", md)   # 支线内容必须在
        self.assertIn("D-main", md)        # 主链内容必须在
        self.assertIn("F", md)
        self.assertIn("支线", md)

    def test_strict_assertion_catches_uncovered_node(self):
        """守住 2：有节点不在主链也不在任何支线时，必须报错而非静默少渲染。"""
        m = _forked_tree()
        m["orphan"] = _node("orphan", "ghost", [], "X")
        with self.assertRaises(AssertionError) as ctx:
            deepseek_tree.render_markdown(m, "13")
        self.assertIn("orphan", str(ctx.exception))

    def test_report_flags_incomplete(self):
        m = _forked_tree()
        m["orphan"] = _node("orphan", "ghost", [], "X")
        self.assertFalse(deepseek_tree.render_report(m, "13")["complete"])
        self.assertTrue(deepseek_tree.render_report(_forked_tree(), "13")["complete"])


class PruneBranches(unittest.TestCase):
    def test_keep_main_only(self):
        kept, report = deepseek_tree.select_branches(_forked_tree(), "main", "13")
        # 裁出的树**自带 root 哨兵**：否则 coverage() 从 root.children 出发
        # 找不到任何节点，prune 的产物就永远无法再 render（回归见
        # test_pruned_tree_is_renderable）。
        self.assertEqual(sorted(kept), ["1", "11", "13", "3", "6", "9", "root"])
        self.assertNotIn("7", kept)
        self.assertEqual(report["dropped_branch_indexes"], [0])

    def test_pruned_tree_is_renderable(self):
        """prune 的产物必须能直接交给 render —— 否则两个模式接不上。"""
        for spec in ("main", "all", ["main", 0]):
            kept, _rep = deepseek_tree.select_branches(_forked_tree(), spec, "13")
            md = deepseek_tree.render_markdown(kept, "13")   # 不许抛
            self.assertIn("▸ n", md)

    def test_keep_all_covers_root_level_fork(self):
        """
        改「第一个问题」会得到两个顶层节点。第二个既不在主链、也不是任何
        主链节点的孩子——曾经因此被 `all` 静默丢掉。
        """
        m = {
            "root": {"id": "root", "parent": None, "children": ["1", "2"],
                     "message": None},
            "1": _node("1", "root", ["3"], "Q1"),
            "2": _node("2", "root", ["4"], "Q1-rewritten"),
            "3": _node("3", "1", [], "A1"),
            "4": _node("4", "2", [], "A1-rewritten"),
        }
        kept, rep = deepseek_tree.select_branches(m, "all", "3")
        self.assertEqual(sorted(n for n in kept if n != "root"),
                         ["1", "2", "3", "4"])
        self.assertEqual(rep["kept_node_count"], 4)

    def test_incomplete_tree_refuses_all(self):
        """树本身不可达时，`all` 不许假装成功——那是丢数据的静默路径。"""
        m = {
            "root": {"id": "root", "parent": None, "children": ["1"],
                     "message": None},
            "1": _node("1", "root", [], "Q1"),
            "ghost": _node("ghost", "nowhere", [], "X"),   # 父悬空
        }
        with self.assertRaises(ValueError) as ctx:
            deepseek_tree.select_branches(m, "all", "1")
        self.assertIn("ghost", str(ctx.exception))

    def test_keep_all(self):
        kept, _rep = deepseek_tree.select_branches(_forked_tree(), "all", "13")
        self.assertIn("7", kept)

    def test_node_id_wins_over_index(self):
        """节点 id 是数字字符串：'7' 应解释为起点节点 n7，而不是第 7 条支线。"""
        _kept, report = deepseek_tree.select_branches(_forked_tree(), ["main", "7"], "13")
        self.assertEqual([b["start"] for b in report["branches"]], ["7"])

    def test_index_by_int(self):
        _kept, report = deepseek_tree.select_branches(_forked_tree(), ["main", 0], "13")
        self.assertEqual([b["index"] for b in report["branches"]], [0])

    def test_unknown_branch_names_the_candidates(self):
        with self.assertRaises(ValueError) as ctx:
            deepseek_tree.select_branches(_forked_tree(), ["main", 99], "13")
        self.assertIn("7", str(ctx.exception))

    def test_source_tree_not_mutated(self):
        m = _forked_tree()
        deepseek_tree.select_branches(m, "main", "13")
        self.assertEqual(m["6"]["children"], ["7", "9"])
        self.assertIn("7", m)

    def test_pruned_tree_reconnects_parent(self):
        _kept, _rep = deepseek_tree.select_branches(_forked_tree(), ["main", "7"], "13")
        kept, _r = deepseek_tree.select_branches(_forked_tree(), ["main", "7"], "13")
        self.assertEqual(kept["7"]["parent"], "6")
        self.assertIn("7", kept["6"]["children"])


class NormalizedSignature(unittest.TestCase):
    """守住 5：跨源比对的假阳性。"""

    def test_derived_fields_do_not_break_equality(self):
        a = {"type": "TOOL_SEARCH",
             "results": [{"url": "https://x/a.pdf", "url_normalized": "https://x/a.pdf",
                          "url_anchor": None, "title": "A"}]}
        b = {"type": "TOOL_SEARCH", "results": [{"url": "https://x/a.pdf", "title": "A"}]}
        self.assertEqual(deepseek_common.frag_signature_normalized(a),
                         deepseek_common.frag_signature_normalized(b))

    def test_citation_anchor_in_url_does_not_break_equality(self):
        a = {"type": "TOOL_SEARCH", "results": [{"url": "https://x/a.pdf#7#22#7"}]}
        b = {"type": "TOOL_SEARCH", "results": [{"url": "https://x/a.pdf"}]}
        self.assertEqual(deepseek_common.frag_signature_normalized(a),
                         deepseek_common.frag_signature_normalized(b))

    def test_signed_path_drift_does_not_break_equality(self):
        a = {"type": "FILE", "files": [{"file_name": "a.png", "signed_path": "/api/AAA"}]}
        b = {"type": "FILE", "files": [{"file_name": "a.png", "signed_path": "/api/BBB"}]}
        self.assertEqual(deepseek_common.frag_signature_normalized(a),
                         deepseek_common.frag_signature_normalized(b))

    def test_url_original_is_content_not_volatile(self):
        """
        `url_original` 是「首次出现时的原始 URL」，是**出处**，
        不是每次请求都变的凭据。把它当易变字段剥掉，会让两份不同文档
        判成相等（假阴性）——那是这个函数最不该犯的错。
        """
        a = {"type": "TOOL_SEARCH", "results": [
            {"url": "https://x/a.pdf", "url_original": "https://x/a.pdf#1",
             "title": "A"}]}
        b = {"type": "TOOL_SEARCH", "results": [
            {"url": "https://x/a.pdf", "url_original": "https://x/b.pdf#2",
             "title": "A"}]}
        self.assertNotEqual(deepseek_common.frag_signature_normalized(a),
                            deepseek_common.frag_signature_normalized(b))
        # 但同一文档的不同引用锚点仍应归一成相等
        c = {"type": "TOOL_SEARCH", "results": [
            {"url": "https://x/a.pdf", "url_original": "https://x/a.pdf#7#22"}]}
        d = {"type": "TOOL_SEARCH", "results": [
            {"url": "https://x/a.pdf", "url_original": "https://x/a.pdf"}]}
        self.assertEqual(deepseek_common.frag_signature_normalized(c),
                         deepseek_common.frag_signature_normalized(d))

    def test_real_differences_still_detected(self):
        a = {"type": "RESPONSE", "content": "hello"}
        b = {"type": "RESPONSE", "content": "hello!"}
        self.assertNotEqual(deepseek_common.frag_signature_normalized(a),
                            deepseek_common.frag_signature_normalized(b))
        c = {"type": "FILE", "files": [{"file_name": "a.png"}]}
        d = {"type": "FILE", "files": [{"file_name": "b.png"}]}
        self.assertNotEqual(deepseek_common.frag_signature_normalized(c),
                            deepseek_common.frag_signature_normalized(d))

    def test_source_objects_not_mutated(self):
        a = {"type": "TOOL_SEARCH",
             "results": [{"url": "https://x/a.pdf#7", "url_normalized": "https://x/a.pdf"}]}
        deepseek_common.frag_signature_normalized(a)
        self.assertEqual(a["results"][0]["url"], "https://x/a.pdf#7")
        self.assertIn("url_normalized", a["results"][0])

    def test_empty_container_equals_absent(self):
        """
        「没有结果」两种写法必须签名相同：一个写 `[]`、一个不写这个键。
        否则跨源比对会报出只因容器写法不同而产生的假差异。
        """
        self.assertEqual(
            deepseek_common.frag_signature_normalized({"type": "TOOL_SEARCH", "content": "a"}),
            deepseek_common.frag_signature_normalized(
                {"type": "TOOL_SEARCH", "content": "a", "results": []}))
        self.assertEqual(
            deepseek_common.frag_signature_normalized({"type": "FILE", "content": "a"}),
            deepseek_common.frag_signature_normalized(
                {"type": "FILE", "content": "a", "files": []}))


class AttachmentNaming(unittest.TestCase):
    """守住 3：同名附件落盘互不覆盖。"""

    def test_same_name_different_sequence(self):
        import extract_deepseek_share as es
        a = es._sequenced_name("43", 1, "frame_0.5s.png")
        b = es._sequenced_name("43", 2, "frame_0.5s.png")
        self.assertNotEqual(a, b)
        self.assertIn("n43", a)
        self.assertIn("01", a)

    def test_same_name_different_node(self):
        import extract_deepseek_share as es
        self.assertNotEqual(es._sequenced_name("23", 3, "image.png"),
                            es._sequenced_name("25", 4, "image.png"))

    def test_illegal_chars_replaced(self):
        import extract_deepseek_share as es
        name = es._sequenced_name("1", 1, 'a/b:c*d?e"f<g>h|i.png')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, name)


class DocumentBoundary(unittest.TestCase):
    """守住 4：文档类附件不该被当成「下载失败」静默处理。"""

    def test_documents_detected(self):
        import extract_deepseek_share as es
        for name in ("a.pdf", "b.docx", "c.xlsx", "d.zip", "note.md"):
            self.assertTrue(es._is_document({"file_name": name}), name)

    def test_image_flag_wins_over_extension(self):
        import extract_deepseek_share as es
        self.assertFalse(es._is_document({"file_name": "x.pdf", "is_image": True}))

    def test_plain_image_is_not_document(self):
        import extract_deepseek_share as es
        self.assertFalse(es._is_document({"file_name": "a.png"}))
        self.assertFalse(es._is_document({"file_name": "a.png", "is_image": True}))


class PrivateSessionDetection(unittest.TestCase):
    def test_private_url_detected(self):
        import deepseek_private as dp
        self.assertTrue(dp.is_private_session_url(
            "https://chat.deepseek.com/a/chat/s/<uuid>"))

    def test_share_url_is_not_private(self):
        import deepseek_private as dp
        self.assertFalse(dp.is_private_session_url(
            "https://chat.deepseek.com/share/<id>"))

    def test_session_id_extraction(self):
        import deepseek_private as dp
        self.assertEqual(
            dp.session_id_from_url("https://chat.deepseek.com/a/chat/s/abc-123?x=1"),
            "abc-123")
        self.assertIsNone(dp.session_id_from_url("abc-123"))

    def test_biz_data_unwrapping(self):
        import deepseek_private as dp
        biz = {"chat_messages": [{"message_id": 1}], "chat_session": {"id": "s"}}
        self.assertEqual(dp.unwrap_biz_data({"data": {"biz_data": biz}}), biz)
        self.assertEqual(dp.unwrap_biz_data(biz), biz)

    def test_missing_biz_data_raises(self):
        import deepseek_private as dp
        with self.assertRaises(dp.PrivateSessionError):
            dp.unwrap_biz_data({"data": {}})


class MalformedTrees(unittest.TestCase):
    """
    守住 8：畸形树不得因为「覆盖率算法看不见」而静默少渲染。

    曾漏掉的一种不一致：节点声称 parent=root，但 root.children 里没有它。
    它既不在主链也不在任何支线，旧的 coverage 完全看不见它，
    `render_markdown` 于是**渲染成功但少了一条消息**。
    """

    def test_child_missing_from_root_children_is_caught(self):
        m = {
            "root": {"id": "root", "parent": None, "children": [], "message": None},
            "1": _node("1", "root", [], "A"),
        }
        with self.assertRaises(AssertionError):
            deepseek_tree.render_markdown(m, "1")

    def test_orphan_root_is_caught(self):
        m = _forked_tree()
        m["99"] = _node("99", None, [], "ORPHAN")
        with self.assertRaises(AssertionError):
            deepseek_tree.render_markdown(m, "13")

    def test_dangling_parent_is_caught(self):
        m = {
            "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
            "1": _node("1", "root", [], "A"),
            "2": _node("2", "ghost", [], "B"),
        }
        with self.assertRaises(AssertionError):
            deepseek_tree.render_markdown(m, "1")

    def test_wellformed_tree_still_renders(self):
        deepseek_tree.render_markdown(_forked_tree(), "13")  # 不该抛

    def test_both_legitimate_top_node_shapes_render(self):
        """
        顶层节点有两种**合法**形态，都不能误报：
          (a) parent = "root"（新适配器 / 导出包）
          (b) parent = None，但被 root.children 收录（历史产物就是这个形态）
        判据只能是「能不能从 root 走到」，不能是「parent 是不是 root」。
        """
        for label, parent in (("parent=root", "root"), ("parent=None", None)):
            m = {
                "root": {"id": "root", "parent": None, "children": ["1"],
                         "message": None},
                "1": _node("1", parent, [], "A"),
            }
            try:
                deepseek_tree.render_markdown(m, "1")
            except AssertionError as exc:
                self.fail("%s 形态被误报为损坏：%s" % (label, exc))

    def test_unreachable_from_root_is_caught(self):
        """parent=None 且 root.children 没收录 → 真的走不到，必须报。"""
        m = {
            "root": {"id": "root", "parent": None, "children": [], "message": None},
            "1": _node("1", None, [], "A"),
        }
        with self.assertRaises(AssertionError):
            deepseek_tree.render_markdown(m, "1")

    def test_private_adapter_top_node_links_to_root(self):
        """顶层消息的 parent 必须记成 root，而不是 None——否则与 root.children 不一致。"""
        import deepseek_private as dp
        msgs = [
            {"message_id": 1, "parent_id": None,
             "fragments": [{"type": "REQUEST", "content": "a"}]},
            {"message_id": 2, "parent_id": 1,
             "fragments": [{"type": "RESPONSE", "content": "b"}]},
        ]
        m = dp.build_mapping(msgs)
        self.assertEqual(m["1"]["parent"], "root")
        self.assertIn("1", m["root"]["children"])

    def test_dangling_parent_is_reported_and_made_reachable(self):
        """
        §12：`parent_id` 指向 API 没返回的消息时，节点原本会孤立
        （从 root 走不到），而适配器的多重集自证**看不见**这种断链
        （两侧同源）。现在上挂到 root 并如实记进 verification。
        """
        import deepseek_private as dp
        msgs = [{"message_id": 1, "parent_id": 42, "inserted_at": 1,
                 "fragments": [{"type": "REQUEST", "content": "a"}]}]
        biz = {"chat_session": {"id": "s", "current_message_id": 1},
               "chat_messages": msgs}
        out = dp.adapt(biz)
        v = out["_meta"]["verification"]
        mp = out["conversations"][0]["raw_mapping"]
        # 1) 可达：不许从 root 走不到
        self.assertIn("1", mp["root"]["children"])
        self.assertEqual(mp["1"]["parent"], "root")
        # 2) 但树确实是断的：不许自称全部通过
        self.assertFalse(v["ok"])
        self.assertEqual(v["dangling_parent_count"], 1)
        self.assertEqual(v["dangling_parent_ids"], ["42"])
        # 3) 覆盖率视角也应当干净（因为它已经可达）
        maint = deepseek_tree.main_path_ids(mp, "1")
        self.assertEqual(deepseek_tree.coverage(
            mp, maint, deepseek_tree.branch_index(mp, maint))[2], [])


class TruncationCheckAgainstRenderer(unittest.TestCase):
    """
    守住 9：`check_no_truncation` 的分块正则必须与渲染器**实际产出**对得上。

    渲染器产出的是 `**▸ n1 · 👤 用户 · <ts>**` 后面**紧跟正文**（无空行）。
    早先正则要求标题后有 `\\n\\n`，结果一块都切不出来，
    整个检查在退化路径上运行——既可能恒 FAIL 也可能恒 PASS。
    """

    def _render(self, mapping, current):
        import deepseek_tree as dt
        return dt.render_markdown(mapping, current)

    def _tree(self, body):
        return {
            "root": {"id": "root", "parent": None, "children": ["1"], "message": None},
            "1": {"id": "1", "parent": "root", "children": [],
                  "message": {"inserted_at": "2026-01-01T10:30:00",
                              "fragments": [{"type": "RESPONSE", "content": body}]}},
        }

    def _write(self, text):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tr.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_matching_content_passes(self):
        import verify_package as vp
        m = self._tree("REAL-BODY-123")
        t = self._write(self._render(m, "1"))
        product = {"conversations": [{"raw_mapping": m, "transcript": [
            {"node_id": "1", "type": "RESPONSE", "content": "REAL-BODY-123"}]}]}
        rep = vp.Report()
        vp.check_no_truncation(rep, product, t)
        self.assertEqual(rep.fails, [])

    def test_tampered_content_fails(self):
        import verify_package as vp
        m = self._tree("REAL-BODY-123")
        t = self._write(self._render(m, "1"))
        product = {"conversations": [{"raw_mapping": m, "transcript": [
            {"node_id": "1", "type": "RESPONSE", "content": "TAMPERED-XYZ"}]}]}
        rep = vp.Report()
        vp.check_no_truncation(rep, product, t)
        self.assertTrue(rep.fails)

    def test_unparseable_transcript_fails_rather_than_passing(self):
        """请求了全录但对不上格式 → 必须 FAIL，不能静默当「没问题」。"""
        import verify_package as vp
        m = self._tree("REAL-BODY-123")
        t = self._write("没有任何节点标记的文本")
        product = {"conversations": [{"raw_mapping": m, "transcript": [
            {"node_id": "1", "type": "RESPONSE", "content": "REAL-BODY-123"}]}]}
        rep = vp.Report()
        vp.check_no_truncation(rep, product, t)
        self.assertTrue(rep.fails)


class PrivateFetchErrorPaths(unittest.TestCase):
    """
    守住 10：抓取失败必须给出**可行动**的原因，不能伪装成「会话不存在」。
    全部用 stub 替换 browser_proxy，不需要真浏览器。
    """

    def _stub(self, *, ready=True, token="TOKEN", body='{"data":{"biz_data":{}}}',
              tab="T1"):
        import browser_proxy as bp
        state = {"closed": []}
        self._saved = {n: getattr(bp, n) for n in
                       ("proxy_ready", "open_tab", "close_tab",
                        "read_local_storage", "in_page_fetch_json", "clear_chunked")}
        bp.proxy_ready = lambda proxy=None: ready
        bp.open_tab = lambda url, proxy=None: tab
        bp.close_tab = lambda t, proxy=None: state["closed"].append(t)
        bp.read_local_storage = lambda *a, **k: token
        bp.in_page_fetch_json = lambda *a, **k: body
        bp.clear_chunked = lambda *a, **k: None
        return state

    def _restore(self):
        import browser_proxy as bp
        for n, v in self._saved.items():
            setattr(bp, n, v)

    def test_proxy_unreachable_is_not_session_missing(self):
        import deepseek_private as dp
        self._stub(ready=False)
        try:
            with self.assertRaises(dp.PrivateSessionError) as ctx:
                dp.fetch_private("abc")
            self.assertIn("代理", str(ctx.exception))
        finally:
            self._restore()

    def test_missing_token_says_not_logged_in(self):
        import deepseek_private as dp
        self._stub(token=None)
        try:
            with self.assertRaises(dp.PrivateSessionError) as ctx:
                dp.fetch_private("abc")
            self.assertIn("登录", str(ctx.exception))
        finally:
            self._restore()

    def test_waf_html_is_reported_as_block(self):
        import deepseek_private as dp
        self._stub(body="<html>Error - Request Blocked</html>")
        try:
            with self.assertRaises(dp.PrivateSessionError) as ctx:
                dp.fetch_private("abc")
            self.assertIn("WAF", str(ctx.exception))
        finally:
            self._restore()

    def test_business_40003_explains_token_not_missing_session(self):
        import deepseek_private as dp
        self._stub(body='{"code":40003,"msg":"INVALID_TOKEN"}')
        try:
            with self.assertRaises(dp.PrivateSessionError) as ctx:
                dp.fetch_private("abc")
            msg = str(ctx.exception)
            self.assertIn("40003", msg)
            self.assertIn("不是「会话不存在」", msg)
        finally:
            self._restore()

    def test_tab_closed_on_error(self):
        import deepseek_private as dp
        state = self._stub(body="<html>blocked</html>")
        try:
            with self.assertRaises(dp.PrivateSessionError):
                dp.fetch_private("abc")
            self.assertEqual(state["closed"], ["T1"])
        finally:
            self._restore()

    def test_keep_tab_leaves_it_open(self):
        import deepseek_private as dp
        state = self._stub(body="<html>blocked</html>")
        try:
            with self.assertRaises(dp.PrivateSessionError):
                dp.fetch_private("abc", keep_tab=True)
            self.assertEqual(state["closed"], [])
        finally:
            self._restore()


class ChunkedFetch(unittest.TestCase):
    """
    守住 7：分块取回不得因「码元 / 码点」或「切断代理对」而静默损坏内容。

    两个坑必须一起解决：
      - JS `slice` 按 UTF-16 码元，Python `len` 按码点 → offset 偏小 → 内容重复
      - 切点落在代理对中间 → Python 拿到落单 surrogate，拼接**不会**复原原字符
    """

    def _fake_eval(self, text):
        import json
        import re

        def fake(target, script, proxy=None, timeout=90):
            if "slice(" not in script:
                return len(text.encode("utf-16-le")) // 2
            lim = int(re.search(r"min\((\d+), s\.length\)", script).group(1))
            start = int(re.search(r"slice\((\d+), end\)", script).group(1))
            u = text.encode("utf-16-le")
            n = len(u) // 2
            end = min(lim, n)
            if end < n:
                cp = int.from_bytes(u[(end - 1) * 2:(end - 1) * 2 + 2], "little")
                if 0xD800 <= cp <= 0xDBFF:
                    end -= 1
            part = u[start * 2:end * 2].decode("utf-16-le", errors="surrogatepass")
            return json.dumps({"t": part, "n": end - start})
        return fake

    def test_astral_chars_survive_any_chunk_size(self):
        import browser_proxy as bp
        text = "ab\U0001F600cd" * 200
        original = bp.evaluate
        bp.evaluate = self._fake_eval(text)
        try:
            for chunk in (3, 7, 100, 5000):
                got = bp.evaluate_chunked("t", "seed", chunk_chars=chunk)
                self.assertEqual(got, text, "chunk=%d 时内容被损坏" % chunk)
        finally:
            bp.evaluate = original

    def test_plain_ascii_survives(self):
        import browser_proxy as bp
        text = "abcdefghij" * 100
        original = bp.evaluate
        bp.evaluate = self._fake_eval(text)
        try:
            self.assertEqual(bp.evaluate_chunked("t", "seed", chunk_chars=7), text)
        finally:
            bp.evaluate = original

    def test_corrupted_chunk_raises_instead_of_being_ingested(self):
        """损坏的分块必须报错——不能当成正常内容吞下去。"""
        import browser_proxy as bp
        original = bp.evaluate

        def fake(target, script, proxy=None, timeout=90):
            if "slice(" not in script:
                return 100
            return "NOT-JSON-GARBAGE"

        bp.evaluate = fake
        try:
            with self.assertRaises(bp.BrowserError) as ctx:
                bp.evaluate_chunked("t", "seed", chunk_chars=10)
            self.assertEqual(ctx.exception.code, "CHUNKED_CHUNK_UNPARSABLE")
        finally:
            bp.evaluate = original

    def test_bool_probe_does_not_truncate_to_one_chunk(self):
        """
        守住 7：`window.__chunked_result = big; true` 会让探针返回 True。
        Python 里 bool 是 int 的子类，`isinstance(True, int)` 为真——
        曾经因此把 total 当成 1，只切出第一块就收工，**静默截断**。
        """
        import browser_proxy as bp
        text = "Z" * 100000
        original = bp.evaluate

        def fake(target, script, proxy=None, timeout=90):
            import json
            import re
            if "slice(" in script:
                lim = int(re.search(r"min\((\d+), s\.length\)", script).group(1))
                start = int(re.search(r"slice\((\d+), end\)", script).group(1))
                end = min(lim, len(text))
                return json.dumps({"t": text[start:end], "n": end - start})
            if "String(window.__chunked_result" in script:
                return len(text)          # 长度查询
            return True                   # 探针：脚本以 `; true` 结尾

        bp.evaluate = fake
        try:
            got = bp.evaluate_chunked("t", "seed")
            self.assertEqual(len(got), len(text), "bool 探针导致静默截断")
        finally:
            bp.evaluate = original


class PackageVerifier(unittest.TestCase):
    """守住 6：请求了不存在的文档必须 FAIL，不能静默跳过。"""

    def test_missing_requested_doc_is_failure_not_skip(self):
        import verify_package as vp
        rep = vp.Report()
        vp.check_requested_docs(rep, ["/definitely/not/here.md"])
        self.assertEqual(len(rep.fails), 1)
        self.assertEqual(len(rep.passes), 0)

    def test_present_doc_passes(self):
        import verify_package as vp
        rep = vp.Report()
        vp.check_requested_docs(rep, [__file__])
        self.assertEqual(len(rep.passes), 1)
        self.assertEqual(rep.fails, [])

    def test_exit_code_two_when_product_missing(self):
        import verify_package as vp
        self.assertEqual(vp.verify(os.path.dirname(__file__),
                                   "/definitely/not/here.json"), 2)

    def _tmp_md(self, text, name="_vp.md"):
        path = os.path.join(os.environ.get("TEMP", "."), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_non_numeric_node_ids_are_understood(self):
        """守住 9：节点 id 不必然是数字，校验器不能用 `\\d+` 把它判成 0 覆盖。"""
        import verify_package as vp
        m = {
            "root": {"id": "root", "parent": None, "children": ["a1"],
                     "message": None},
            "a1": {"id": "a1", "parent": "root", "children": [],
                   "message": {"inserted_at": "2026-01-01T00:00:00",
                               "fragments": [{"type": "REQUEST", "content": "hi"}]}},
        }
        product = {"conversations": [{"raw_mapping": m, "transcript": []}]}
        path = self._tmp_md(
            "# x\n\n**▸ na1 · 👤 用户 · 2026-01-01 00:00**\nhi\n")
        try:
            rep = vp.Report()
            vp.check_transcript_coverage(rep, product, path)
            self.assertEqual(rep.fails, [], "非数字 id 被误判：%r" % (rep.fails,))
        finally:
            os.remove(path)

    def test_empty_transcript_is_not_a_vacuous_pass(self):
        """守住 10：空全录不能出现「无重复（0 条）」这种绿灯。"""
        import verify_package as vp
        product = {"conversations": [{
            "raw_mapping": {
                "root": {"id": "root", "parent": None, "children": ["1"],
                         "message": None},
                "1": {"id": "1", "parent": "root", "children": [],
                      "message": {"inserted_at": "2026-01-01T00:00:00",
                                  "fragments": [{"type": "REQUEST", "content": "hi"}]}},
            },
            "transcript": []}]}
        path = self._tmp_md("# empty\n")
        try:
            rep = vp.Report()
            vp.check_transcript_coverage(rep, product, path)
            self.assertFalse([m for m in rep.passes if "无重复" in m])
            self.assertTrue(rep.fails)
        finally:
            os.remove(path)

    def test_empty_mainline_fails_instead_of_alternating_vacuously(self):
        """守住 7：单线是空的（例如误把 prune 产物当单线）必须 FAIL。"""
        import verify_package as vp
        path = os.path.join(os.environ.get("TEMP", "."), "_vp_mainline.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"messages": []}, f)
        try:
            rep = vp.Report()
            vp.check_mainline(rep, {"conversations": [{}]}, path)
            self.assertTrue(rep.fails)
            self.assertFalse([m for m in rep.passes if "交替" in m])
        finally:
            os.remove(path)

    def test_placeholder_detection(self):
        import verify_package as vp
        rep = vp.Report()
        path = os.path.join(os.environ.get("TEMP", "."), "_vp_placeholder.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("这里有个 TODO 没做")
        try:
            vp.check_placeholders(rep, [path])
            self.assertEqual(len(rep.fails), 1)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()

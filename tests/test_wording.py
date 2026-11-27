# -*- coding: utf-8 -*-
"""Card text rules: no paths or code on a card, short titles, the PM's limits.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import pm, wording  # noqa: E402
from docket.wording import jargon, sentences, short_title, width  # noqa: E402


class Jargon(unittest.TestCase):
    def test_paths_files_and_code_are_caught_whole(self):
        for text, piece in (
            ("修 Config/Signing.local.xcconfig 里的 Team ID", "Config/Signing.local.xcconfig"),
            ("代码在 项目/fitness-app 下", "/fitness-app"),
            ("跑 tools/build.sh 看看", "tools/build.sh"),
            ("按 docs/INSTALL_DEVICE.md 装", "docs/INSTALL_DEVICE.md"),
            ("late_seconds 超了", "late_seconds"),
            ("调用 build() 失败", "build()"),
            ("看 https://example.com/x", "https://example.com/x"),
            ("先 sudo make install", "sudo"),
            ("改 Models.swift", "Models.swift"),
            ("放进 docs/ 目录", "docs/"),
            ("见 app/Sources/Docket", "app/Sources/Docket"),
        ):
            with self.subTest(text=text):
                self.assertIn(piece, [p for p, _ in jargon(text)])

    def test_plain_words_pass(self):
        for text in ("签名配置缺一项，我来补。", "A/B 两套方案都行", "iOS/安卓都支持", "第 3/5 轮", "v0.1 已装到手机",
                     "描述文件（装到手机上的授权文件）缺了", "用 OpenCode(编码代理) 跑", "Node.js 版本太旧", "iPhone 上能打开",
                     "The signing settings are missing one value; I will fill it in."):
            with self.subTest(text=text):
                self.assertEqual(jargon(text), [])

    def test_sentences(self):
        self.assertEqual(sentences("签名配置缺一项。我来补。"), 2)
        self.assertEqual(sentences("v0.1 已装好，能用"), 1)
        self.assertEqual(sentences("This piece is done; go on or wrap up?"), 1)
        self.assertEqual(sentences("Done. Next is the phone."), 2)


class Titles(unittest.TestCase):
    def test_long_titles_are_cut_at_a_word_with_an_ellipsis(self):
        self.assertEqual(short_title("健身 FitLog v0.1 原生 iPhone 训练应用"), "健身 FitLog v0.1 原生…")
        self.assertEqual(short_title("Add an impact-factor sort to the journal monitor"), "Add an impact-factor…")
        cut = short_title("给顶刊监控加一个按影响因子排序的筛选功能并上线")
        self.assertTrue(cut.endswith("…") and width(cut) <= 24)

    def test_short_titles_are_left_alone(self):
        for title in ("Triad 手机客户端 第一版", "DayPlan 三面卡片与落库", "健身记录 App"):
            self.assertEqual(short_title(title), title)


class Limits(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pm, "LOCALE", "zh")
        patcher.start()
        self.addCleanup(patcher.stop)

    def card(self, **fields):
        return {"kind": "report", "progress": "第一版做好了。", "decisions": [], "decided_for_you": [], **fields}

    def test_a_plain_two_sentence_card_passes(self):
        self.assertEqual(pm.validate_card(self.card(next="要我接着做第二版吗？")), [])

    def test_each_rule_is_named_with_the_sentence(self):
        cases = (
            (self.card(progress="第一版做好了。模拟器能跑。"), "must be one sentence"),
            (self.card(progress="甲" * 41), "over the limit of 40"),
            (self.card(next="tools/build.sh 能跑"), "a file name"),
            (self.card(decided_for_you=["改了 late_seconds"]), "decided_for_you[0] contains a variable"),
            (self.card(decisions=[{"id": "d1", "question": "怎么办？", "options": [
                {"id": "a", "label": "现在就装到你手机上", "recommended": True}, {"id": "b", "label": "不装", "recommended": False}]}]), "label is 9"),
            (self.card(decisions=[{"id": "d1", "question": "怎么办？", "options": [
                {"id": c, "label": c, "recommended": c == "a"} for c in "abcd"]}]), "2–3 options"),
        )
        for card, error in cases:
            with self.subTest(error=error):
                errors = pm.validate_card(card)
                self.assertTrue(any(error in e for e in errors), errors)

    def test_a_close_is_checked_too(self):
        errors = pm.validate_action({"action": "close", "conclusion": "做完了，结果在 docs/RESULT.md", "remaining": ["补 Signing.xcconfig"]})
        self.assertEqual(len(errors), 2)
        self.assertEqual(pm.validate_action({"action": "close", "conclusion": "做完了，手机上能用", "remaining": ["签名配置还差一项"]}), [])

    def test_the_relay_to_the_coding_agent_may_be_exact(self):
        self.assertEqual(pm.validate_action({"action": "relay", "message": "Fix Config/Signing.local.xcconfig, run tools/build.sh"}), [])

    def test_the_prompt_carries_the_rules(self):
        rules = pm.rules() + pm.card_format()
        for said in ("file path", "签名配置缺一项", "描述文件（装到手机上的授权文件）", "at most 40", "together at most 80", "2–3 short options"):
            self.assertIn(said, rules)

    def test_body_joins_the_two_sentences(self):
        self.assertEqual(wording.body("做好了", "要接着做吗？"), "做好了。要接着做吗？")
        self.assertEqual(wording.body("Done", "Go on?"), "Done. Go on?")
        self.assertEqual(wording.body("做好了。", None), "做好了。")


if __name__ == "__main__":
    unittest.main()

"""Candidate prompt and output-format regressions."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import generate
import styles


class GenerationPromptTests(unittest.TestCase):
    def test_reply_length_has_no_thirty_character_limit(self):
        self.assertNotIn("30 个字", generate.PROMPT_ONE)
        self.assertIn("长度按内容需要决定", generate.PROMPT_ONE)

    def test_natural_communication_combines_human_writing_and_nvc(self):
        instruction = styles.BUILTIN["自然沟通"]
        for phrase in (
            "非暴力沟通", "具体情况", "感受", "需要", "请求",
            "AI 腔", "不是…而是…", "自问自答", "强行排比", "破折号",
        ):
            self.assertIn(phrase, instruction)

    def test_candidate_punctuation_is_normalized(self):
        parsed = generate.Generator._parse(
            "收到，我今晚看一下。\n"
            "高情商话术：你现在很着急；我明白！\n"
            "需要我明早回你吗？")
        self.assertEqual(parsed, [
            "收到,我今晚看一下",
            "你现在很着急,我明白",
            "需要我明早回你吗",
        ])
        for reply in parsed:
            self.assertFalse(reply.endswith(","))

    def test_configurable_candidate_count_reaches_prompt_and_parser(self):
        generator = generate.Generator()
        captured = []
        generator._call = lambda prompt, on_delta=None: (
            captured.append(prompt) or "第一条\n第二条\n第三条\n第四条")
        replies, error = generator._one_tone(
            "在吗", "闲聊", "自然沟通", candidate_count=3)
        self.assertFalse(error)
        self.assertEqual(replies, ["第一条", "第二条", "第三条"])
        self.assertIn("请写 3 条回复候选", captured[0])
        self.assertIn("只输出 3 行", captured[0])

    def test_single_candidate_prompt_does_not_ask_same_line_to_escalate(self):
        generator = generate.Generator()
        captured = []
        generator._call = lambda prompt, on_delta=None: captured.append(prompt) or "收到"
        generator._one_tone("在吗", "闲聊", "自然沟通", candidate_count=1)
        self.assertIn("这一条要稳妥", captured[0])
        self.assertNotIn("最后一条可以更皮", captured[0])

    def test_refine_one_candidate_keeps_context_and_single_reply(self):
        generator = generate.Generator()
        prompts = []
        generator._call = lambda prompt, on_delta=None: (
            prompts.append(prompt) or '好的，我明天处理。\n多余第二条')
        revised = generator.refine_candidate('明天可以吗', '我会在明天处理', '缩短')
        self.assertEqual(revised, '好的,我明天处理')
        self.assertIn('原候选回复：我会在明天处理', prompts[0])
        self.assertIn('不得增加新事实', prompts[0])
        self.assertIn('尽量缩短', prompts[0])
        with self.assertRaises(ValueError):
            generator.refine_candidate('明天可以吗', '我会在明天处理', '添加事实')


if __name__ == "__main__":
    unittest.main()

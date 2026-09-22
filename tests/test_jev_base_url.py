import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from generate import (_endpoint, base_has_version_segment, base_is_verbatim_action,
                      jev_request_url)
import judge_jev
import settings_config


# #42: TYPESAFE_BASE_URL / OPENAI_BASE_URL 的拼接规则。全部离线——不发网络请求
# （http_post_json 被拦截）、不读凭据（base/key 显式传参）。
class EndpointCompositionTests(unittest.TestCase):
    def test_generation_endpoint_unchanged(self):
        # 生成层两态规则的重构守卫：这些 URL 任何一条变了都是回归
        cases = [
            ('https://api.deepseek.com', 'openai', 'https://api.deepseek.com/v1/chat/completions'),
            ('https://api.deepseek.com/v1', 'openai', 'https://api.deepseek.com/v1/chat/completions'),
            ('https://open.bigmodel.cn/api/paas/v4', 'openai',
             'https://open.bigmodel.cn/api/paas/v4/chat/completions'),
            ('https://api.anthropic.com', 'anthropic', 'https://api.anthropic.com/v1/messages'),
            ('https://api.anthropic.com/v1', 'anthropic', 'https://api.anthropic.com/v1/messages'),
        ]
        for base, api, expect in cases:
            with self.subTest(base=base, api=api):
                self.assertEqual(_endpoint(base, api), expect)

    def test_version_segment_detection(self):
        self.assertTrue(base_has_version_segment('https://gw.example.com/v1'))
        self.assertTrue(base_has_version_segment('https://gw.example.com/v1/'))
        self.assertTrue(base_has_version_segment('https://gw.example.com/api/v4'))
        self.assertFalse(base_has_version_segment('https://api.typesafe.ai'))
        self.assertFalse(base_has_version_segment('https://gw.example.com/api'))
        self.assertFalse(base_has_version_segment(''))
        self.assertFalse(base_has_version_segment(None))

    def test_verbatim_action_detection(self):
        self.assertTrue(base_is_verbatim_action('https://ai-gateway.vercel.sh/v1/evaluate'))
        self.assertTrue(base_is_verbatim_action('https://ai-gateway.vercel.sh/v1/evaluate/'))
        self.assertFalse(base_is_verbatim_action('https://gw.example.com/v1'))
        self.assertFalse(base_is_verbatim_action('https://gw.example.com/api'))
        self.assertFalse(base_is_verbatim_action('https://api.typesafe.ai'))
        self.assertFalse(base_is_verbatim_action(None))

    def test_jev_request_url_three_way_rule(self):
        cases = [
            ('', '/v1/systemone'),                     # 退化输入：裸动作路径，不崩
            ('https://api.typesafe.ai', 'https://api.typesafe.ai/v1/systemone'),
            ('https://gw.example.com/v1', 'https://gw.example.com/v1/systemone'),   # 不再 /v1/v1/…
            ('https://gw.example.com', 'https://gw.example.com/v1/systemone'),
            ('https://gw.example.com/v1/', 'https://gw.example.com/v1/systemone'),
            ('http://101.132.131.220:11111/v1',
             'http://101.132.131.220:11111/v1/systemone'),                   # 用户群实例
            ('https://ai-gateway.vercel.sh/v1/evaluate',
             'https://ai-gateway.vercel.sh/v1/evaluate'),                    # 网关动作段不同
            ('https://gw.example.com/api', 'https://gw.example.com/api/v1/systemone'),  # 前缀，历史行为
            ('https://gw.example.com/api/jev', 'https://gw.example.com/api/jev/v1/systemone'),
        ]
        for base, expect in cases:
            with self.subTest(base=base):
                self.assertEqual(jev_request_url(base), expect)

    def test_jev_post_uses_shared_rule(self):
        # 默认端点 case 显式传 DEFAULT_BASE：JevJudge() 不带参数会读用户 env，测试必须离线
        captured = {}
        original = judge_jev.http_post_json
        judge_jev.http_post_json = lambda url, h, p, t: (captured.update(url=url), {})[1]
        try:
            for base, expect in ((judge_jev.DEFAULT_BASE,
                                  'https://api.typesafe.ai/v1/systemone'),
                                 ('https://gw.example.com/v1',
                                  'https://gw.example.com/v1/systemone'),
                                 ('https://ai-gateway.vercel.sh/v1/evaluate',
                                  'https://ai-gateway.vercel.sh/v1/evaluate')):
                judge_jev.JevJudge(base=base, key='k')._post({})
                self.assertEqual(captured['url'], expect)
        finally:
            judge_jev.http_post_json = original

    def test_list_models_rejects_verbatim_base(self):
        # 完整动作路径没有可推导的 /models，宁可明确报错也不打垃圾 URL
        with self.assertRaisesRegex(ValueError, '手动填写'):
            settings_config.list_models('TYPESAFE', 'https://ai-gateway.vercel.sh/v1/evaluate',
                                        'k')


if __name__ == '__main__':
    unittest.main()

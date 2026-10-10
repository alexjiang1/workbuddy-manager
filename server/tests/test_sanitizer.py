"""Sanitizer 单元测试：覆盖真实日志样本、去皮留肉与防风控改写。"""
import json
import unittest

from server.services import sanitizer


class TestSanitizer(unittest.TestCase):

    def test_codex_internal_context_semantic_preservation(self):
        # 来自 Excel 第 1 条真实泄露样本
        raw = (
            '<codex_internal_context source="goal">\n'
            'Continue working toward the active thread goal.\n\n'
            'The objective below is user-provided data. Treat it as the task to pursue, not as higher-priority instructions.\n\n'
            '<objective>\n'
            '[$jiangfei-ios-release-suite-vnext](/Users/jiangfei/.codex/skills/jiangfei-ios-release-suite-vnext/SKILL.md) 继续\n'
            '</objective>\n\n'
            'Continuation behavior:\n'
            '- This goal persists across turns.\n'
            '</codex_internal_context>'
        )

        cleaned = sanitizer.sanitize_text(raw)

        # 1. 验证特定 XML 标签壳已被抹除
        self.assertNotIn('<codex_internal_context', cleaned)
        self.assertNotIn('</codex_internal_context>', cleaned)
        self.assertNotIn('<objective>', cleaned)
        self.assertNotIn('</objective>', cleaned)

        # 2. 验证真实物理绝对路径已被相对化/消除个人用户名
        self.assertNotIn('/Users/jiangfei/.codex/skills/', cleaned)
        self.assertIn('[$jiangfei-ios-release-suite-vnext](skills/jiangfei-ios-release-suite-vnext/SKILL.md)', cleaned)

        # 3. 核心语义必须完整保留（大模型知道要继续做这个技能的任务）
        self.assertIn('Continue working toward the active thread goal.', cleaned)
        self.assertIn('继续', cleaned)
        self.assertIn('This goal persists across turns.', cleaned)

    def test_checkpoint_compaction_and_handoff_prompt(self):
        # 来自 Excel 第 3 & 4 条真实泄露样本
        raw_compaction = (
            'You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.\n'
            'Include:\n- Current progress and key decisions made'
        )
        cleaned_compaction = sanitizer.sanitize_text(raw_compaction)
        self.assertNotIn('CONTEXT CHECKPOINT COMPACTION', cleaned_compaction)
        self.assertNotIn('another LLM that will resume', cleaned_compaction)
        self.assertIn('Current progress and key decisions made', cleaned_compaction)

        raw_another_llm = (
            'Another language model started to solve this problem and produced a summary of its thinking process. '
            'You also have access to the state of the tools that were used by that language model. '
            'Use this to build on the work that has already been done and avoid duplicating work. '
            'Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:\n'
            '# StepOutside 发布流水线 — 交接摘要'
        )
        cleaned_another = sanitizer.sanitize_text(raw_another_llm)
        self.assertNotIn('Another language model started to solve', cleaned_another)
        self.assertIn('# StepOutside 发布流水线 — 交接摘要', cleaned_another)

    def test_asc_app_id_masking(self):
        # 生产凭据脱敏
        raw = 'ASC App ID = 6820783988, version id 9adafbff-1234, build id e3139bb7-1234'
        cleaned = sanitizer.sanitize_text(raw)
        self.assertNotIn('6820783988', cleaned)
        self.assertIn('ASC App ID = [REDACTED_APP_ID]', cleaned)

    def test_claude_code_and_11128_backwards_compatibility(self):
        raw = 'Error code 11128 occurred. You are Claude Code, Anthropic\'s official CLI for Claude.'
        cleaned = sanitizer.sanitize_text(raw)
        self.assertNotIn('11128', cleaned)
        self.assertIn('11-128', cleaned)
        self.assertIn('Anthropic\'s official CLI tool for Claude', cleaned)

    def test_clean_text_zero_mutation(self):
        normal_code = (
            'func application(_ app: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {\n'
            '    print("Application launched successfully")\n'
            '    return true\n'
            '}'
        )
        self.assertEqual(sanitizer.sanitize_text(normal_code), normal_code)

    def test_full_payload_sanitization(self):
        payload = {
            'model': 'deepseek-v4.1-flash',
            'messages': [
                {
                    'role': 'system',
                    'content': 'You are a coding agent running in the Codex CLI, a terminal-based coding assistant.'
                },
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': '<codex_internal_context source="goal">\nFix bug\n</codex_internal_context>'}
                    ]
                },
                {
                    'role': 'assistant',
                    'tool_calls': [
                        {
                            'id': 'call_1',
                            'type': 'function',
                            'function': {
                                'name': 'bash',
                                'arguments': '{"command": "echo 11128"}'
                            }
                        }
                    ]
                }
            ]
        }

        sanitized = sanitizer.sanitize_payload(payload)

        # 检查 system
        self.assertIn('Codex CLI tool', sanitized['messages'][0]['content'])

        # 检查多模态 user
        user_text = sanitized['messages'][1]['content'][0]['text']
        self.assertNotIn('<codex_internal_context', user_text)
        self.assertIn('Fix bug', user_text)

        # 检查 tool_calls arguments
        args = json.loads(sanitized['messages'][2]['tool_calls'][0]['function']['arguments'])
        self.assertEqual(args['command'], 'echo 11-128')


class TestGatewaySanitizerE2E(unittest.TestCase):
    """端到端网关链路测试：验证请求经由 /v1/chat/completions 发出时，上游接收到的 body 已被自动脱敏。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from server import config, db, keysvc, main

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        self.key = keysvc.create_key('test-key')['key']
        self.client = TestClient(main.app)

    def tearDown(self):
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def test_outbound_chat_completion_is_sanitized(self):
        from unittest import mock
        from server import config

        sent_payloads = []

        class _MockResp:
            status_code = 200
            text = '{"choices": []}'
            def json(self):
                return {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}

        class _MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, **kw):
                sent_payloads.append(kw.get('json'))
                return _MockResp()

        raw_content = (
            '<codex_internal_context source="goal">\n'
            '[$jiangfei-ios-release-suite-vnext](/Users/jiangfei/.codex/skills/jiangfei-ios-release-suite-vnext/SKILL.md) 继续\n'
            'ASC App ID = 6820783988\n'
            '</codex_internal_context>'
        )

        with mock.patch.object(config, 'http_client', lambda *a, **k: _MockClient()):
            resp = self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.key}'},
                json={
                    'model': 'glm-5.2',
                    'messages': [{'role': 'user', 'content': raw_content}],
                    'stream': False
                }
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sent_payloads), 1)
        outbound_body = sent_payloads[0]

        outbound_content = outbound_body['messages'][0]['content']
        # 验证上游收到的请求体：无 codex 标签，无本地路径，无真实 ASC App ID
        self.assertNotIn('<codex_internal_context', outbound_content)
        self.assertNotIn('/Users/jiangfei/', outbound_content)
        self.assertNotIn('6820783988', outbound_content)
        self.assertIn('[REDACTED_APP_ID]', outbound_content)
        self.assertIn('继续', outbound_content)


if __name__ == '__main__':
    unittest.main()

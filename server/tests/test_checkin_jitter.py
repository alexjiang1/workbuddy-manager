"""批量签到时间抖动（Jitter）机制测试。

验证：
1. `config.checkin_jitter_delay()` 在配置区间内随机采样；
2. 关闭抖动（MAX <= 0）时返回 0.0；
3. `POST /api/accounts/checkin-all` 发起签到前对各账号施加独立的随机时间抖动；
4. 跳过（国际版/今日已签）的账号不执行抖动与网络请求。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase, mock

from fastapi.testclient import TestClient

from server import config
from server.main import app
from server.services import realm, tencent, wb2api

CN_UID = '100000001'
CN2_UID = '100000002'
GL_UID = '200000001'


def _acc(f: str, uid: str, name: str) -> dict:
    return {'file': f, 'uid': uid, 'nickname': name, 'realm': 'cn'}


def _raw(uid: str, realm_tag: str) -> dict:
    domain = 'workbuddy.ai' if realm_tag == 'global' else 'codebuddy.cn'
    return {
        'account': {'uid': uid, 'enterpriseId': ''},
        'auth': {'accessToken': f'tok_{uid}', 'domain': domain, 'realm': realm_tag},
    }


class CheckinJitterConfigTest(TestCase):
    def test_jitter_sampled_within_range(self) -> None:
        """抖动值严格落在 [MIN, MAX] 区间。"""
        with mock.patch.object(config, 'CHECKIN_JITTER_MIN', 0.5), \
             mock.patch.object(config, 'CHECKIN_JITTER_MAX', 1.5):
            samples = [config.checkin_jitter_delay() for _ in range(50)]
            self.assertTrue(all(0.5 <= s <= 1.5 for s in samples))
            # 应该有离散浮点数变化，不能是固定常量
            self.assertGreater(len(set(samples)), 10)

    def test_jitter_disabled_when_max_zero(self) -> None:
        """当 MAX <= 0 时关闭抖动，返回 0.0。"""
        with mock.patch.object(config, 'CHECKIN_JITTER_MIN', 0.0), \
             mock.patch.object(config, 'CHECKIN_JITTER_MAX', 0.0):
            self.assertEqual(config.checkin_jitter_delay(), 0.0)


class CheckinJitterRouteTest(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE)
        root = Path(self._tmp.name)
        config.AUTH_DIR = root / 'auths'
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'

        from server import db, security
        db._conn = None
        db.connect()
        self._db = db

        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })

        self.c = TestClient(app)
        login_res = self.c.post('/api/login', json={'username': 'admin', 'password': 'p'})
        self.assertEqual(login_res.status_code, 200)

    def tearDown(self) -> None:
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE = self._orig
        self._tmp.cleanup()

    def test_checkin_all_executes_jitter_sleep(self) -> None:
        """批量签到在发起 tencent.checkin 之前必须执行时间抖动 sleep。"""
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙')]

        sleep_calls: list[float] = []

        async def fake_sleep(sec: float):
            sleep_calls.append(sec)

        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        def _read(f: str, auth_dir=None) -> dict:
            uid = CN_UID if 'a' in f else CN2_UID
            return _raw(uid, 'cn')

        with mock.patch('asyncio.sleep', side_effect=fake_sleep), \
             mock.patch.object(config, 'CHECKIN_JITTER_MIN', 0.5), \
             mock.patch.object(config, 'CHECKIN_JITTER_MAX', 1.5), \
             mock.patch.object(wb2api, 'list_auth_accounts', return_value=accounts), \
             mock.patch.object(wb2api, 'read_account_file', side_effect=_read), \
             mock.patch.object(tencent, 'checkin', checkin):
            r = self.c.post('/api/accounts/checkin-all')

        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out['succeeded'], 2)
        self.assertEqual(checkin.call_count, 2)
        # 两个账号都执行了抖动延迟，且延迟各不相同
        self.assertEqual(len(sleep_calls), 2)
        self.assertTrue(all(0.5 <= s <= 1.5 for s in sleep_calls))

    def test_skipped_and_already_accounts_do_not_jitter(self) -> None:
        """国际版（跳过）账号不执行抖动 sleep。"""
        accounts = [_acc('workbuddy-g.json', GL_UID, '国际号')]
        sleep_calls: list[float] = []

        async def fake_sleep(sec: float):
            sleep_calls.append(sec)

        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        with mock.patch('asyncio.sleep', side_effect=fake_sleep), \
             mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True, 'chat_base': '',
                                             'billing_base': ''}), \
             mock.patch.object(realm, 'invalidate'), \
             mock.patch.object(wb2api, 'list_auth_accounts', return_value=accounts), \
             mock.patch.object(wb2api, 'read_account_file',
                               return_value=_raw(GL_UID, 'global')), \
             mock.patch.object(tencent, 'checkin', checkin):
            r = self.c.post('/api/accounts/checkin-all')

        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out['skipped'], 1)
        self.assertEqual(checkin.call_count, 0)
        self.assertEqual(len(sleep_calls), 0, '跳过的账号不应执行抖动 sleep')

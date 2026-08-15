"""登录与「登录后取配置」的测试（不含 GUI，到处都能跑）。

这一层管三件事，每件都能把人挡在门外或者放错人进来，所以全都要测：
① 本机账号——密码不存明文、错密码进不来、首次运行认得出来；
② 后端 provider——契约字段解析、坏数据不崩、错误翻成人话；
③ 两份配置的合并——后端下发打底、本机改的盖在上面，谁也别把谁吃掉。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wxclient.ui import auth, settings_store


class LocalAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.provider = auth.LocalAccountProvider(self.dir)

    def test_a_fresh_machine_asks_you_to_set_an_account(self) -> None:
        self.assertTrue(self.provider.needs_setup())

        self.provider.create("zhangsan", "hunter2")

        self.assertFalse(self.provider.needs_setup(), "设过之后就不该再问")

    def test_the_password_is_never_stored_in_the_clear(self) -> None:
        """★ 本机账号文件是明摆在磁盘上的，存明文等于没有密码。"""
        self.provider.create("zhangsan", "hunter2")

        raw = auth.account_path(self.dir).read_text(encoding="utf-8")

        self.assertNotIn("hunter2", raw)
        record = json.loads(raw)
        self.assertTrue(record["salt"], "要有盐——否则同样的密码在每台机器上哈希都一样")
        self.assertNotEqual(record["password"], record["salt"])

    def test_the_right_password_gets_in_and_the_wrong_one_does_not(self) -> None:
        self.provider.create("zhangsan", "hunter2")

        identity = self.provider.login("zhangsan", "hunter2")
        self.assertEqual(identity.account, "zhangsan")

        with self.assertRaises(auth.AuthError):
            self.provider.login("zhangsan", "hunter3")
        with self.assertRaises(auth.AuthError):
            self.provider.login("lisi", "hunter2")

    def test_setting_an_account_twice_is_refused(self) -> None:
        """★ 否则谁先摸到这台机器谁就能把别人的账号顶掉。"""
        self.provider.create("zhangsan", "hunter2")

        with self.assertRaises(auth.AuthError):
            self.provider.create("lisi", "hunter2")

    def test_a_too_short_password_is_refused_up_front(self) -> None:
        with self.assertRaises(auth.AuthError):
            self.provider.create("zhangsan", "123")

    def test_remembering_only_ever_stores_the_account_name(self) -> None:
        auth.remember_account(self.dir, "zhangsan")

        raw = (self.dir / auth.UI_STATE_FILE).read_text(encoding="utf-8")

        self.assertIn("zhangsan", raw)
        self.assertEqual(auth.last_account(self.dir), "zhangsan")
        auth.forget_account(self.dir)
        self.assertEqual(auth.last_account(self.dir), "")

    def test_a_corrupt_account_file_says_so_instead_of_throwing_json_at_you(self) -> None:
        auth.account_path(self.dir).write_text("{这不是 json", encoding="utf-8")

        with self.assertRaises(auth.AuthError) as caught:
            self.provider.login("zhangsan", "hunter2")

        self.assertIn("读不了", str(caught.exception))


class RemoteProviderTests(unittest.TestCase):
    """后端还不存在，所以这里对着契约测——后端照着 ``REMOTE_CONTRACT`` 实现就能接上。"""

    def _provider_returning(self, payload: object, status: int = 200) -> auth.RemoteProvider:
        import io
        import urllib.error
        import urllib.request
        from unittest import mock

        provider = auth.RemoteProvider("https://example.test/api")
        body = json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            if status != 200:
                raise urllib.error.HTTPError(
                    request.full_url, status, "nope", {}, io.BytesIO(body)  # type: ignore[arg-type]
                )
            return mock.MagicMock(
                __enter__=lambda _self: mock.Mock(read=lambda: body),
                __exit__=lambda *_: False,
            )

        patcher = mock.patch.object(urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def test_login_reads_who_you_are_out_of_the_response(self) -> None:
        provider = self._provider_returning({
            "token": "tok-1", "display_name": "张三", "tenant_name": "某某劳务",
            "expires_at": "2026-08-15T12:00:00Z",
        })

        identity = provider.login("zhangsan", "hunter2")

        self.assertEqual((identity.token, identity.display_name, identity.tenant_name),
                         ("tok-1", "张三", "某某劳务"))

    def test_a_login_without_a_token_is_a_failure_not_a_success(self) -> None:
        """★ 后端返回 200 但没给票，照样是进不去——别让界面以为登上了。"""
        provider = self._provider_returning({"display_name": "张三"})

        with self.assertRaises(auth.AuthError):
            provider.login("zhangsan", "hunter2")

    def test_401_becomes_a_sentence_a_human_can_act_on(self) -> None:
        provider = self._provider_returning({"error": {"message": "no"}}, status=401)

        with self.assertRaises(auth.AuthError) as caught:
            provider.login("zhangsan", "bad")

        self.assertEqual(str(caught.exception), "账号或密码不对")

    def test_config_takes_only_wxclient_keys(self) -> None:
        """★ 后端多下发的键不该悄悄变成本机环境变量——那是一条没人看管的注入路径。"""
        provider = self._provider_returning({
            "settings": {"WXCLIENT_UI_ACCOUNTS": "zk_weixin", "PATH": "/evil/bin"},
        })

        config = provider.fetch_config(auth.Identity(account="zhangsan", token="tok-1"))

        self.assertEqual(config.values, {"WXCLIENT_UI_ACCOUNTS": "zk_weixin"})
        self.assertEqual(config.source, "后端")

    def test_the_client_no_longer_asks_for_or_parses_plans(self) -> None:
        """★ 计划归 AI 端（用户 2026-08-15 裁决），桌面端连解析都不该留着——
        留一个没人调的解析器，下一个人会以为「计划是从这条路下来的」。
        """
        self.assertFalse(hasattr(auth, "parse_plans"))
        self.assertFalse(hasattr(auth, "Plan"))
        self.assertNotIn("plans", auth.REMOTE_CONTRACT)

    def test_the_provider_is_chosen_by_whether_a_backend_is_configured(self) -> None:
        data_dir = Path(tempfile.mkdtemp())

        self.assertIsInstance(auth.build_provider(data_dir, {}), auth.LocalAccountProvider)
        self.assertIsInstance(
            auth.build_provider(data_dir, {"WXCLIENT_UI_LOGIN_API_URL": "https://x.test"}),
            auth.RemoteProvider,
        )


class ConfigLayeringTests(unittest.TestCase):
    """后端下发的和本机填的，两份怎么合。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def test_local_wins_over_remote_but_only_where_local_actually_has_a_value(self) -> None:
        settings_store.save_remote(self.dir, {
            "WXCLIENT_UI_ACCOUNTS": "后端给的", "WXCLIENT_GATEWAY_AI_WS_URL": "wss://后端/ws",
        })
        settings_store.save(self.dir, {"WXCLIENT_UI_ACCOUNTS": "本机改的"})

        merged = settings_store.merged(self.dir)

        self.assertEqual(merged["WXCLIENT_UI_ACCOUNTS"], "本机改的")
        self.assertEqual(merged["WXCLIENT_GATEWAY_AI_WS_URL"], "wss://后端/ws",
                         "本机没填的键要照用后端那份")

    def test_the_config_page_can_tell_which_rows_the_machine_overrode(self) -> None:
        settings_store.save_remote(self.dir, {
            "WXCLIENT_UI_ACCOUNTS": "后端给的", "WXCLIENT_GATEWAY_AI_WS_URL": "wss://后端/ws",
        })
        settings_store.save(self.dir, {
            "WXCLIENT_UI_ACCOUNTS": "本机改的", "WXCLIENT_GATEWAY_AI_WS_URL": "wss://后端/ws",
        })

        overridden = settings_store.overridden(self.dir)

        self.assertEqual(overridden, {"WXCLIENT_UI_ACCOUNTS": "后端给的"},
                         "只有真的不一样的那行才算「本机已改」")

    def test_a_new_push_replaces_the_remote_file_wholesale(self) -> None:
        """★ 合并会让被后端删掉的键在本机永远活下去。"""
        settings_store.save_remote(self.dir, {"WXCLIENT_UI_ACCOUNTS": "a", "WXCLIENT_X": "留不得"})

        settings_store.save_remote(self.dir, {"WXCLIENT_UI_ACCOUNTS": "a"})

        self.assertEqual(settings_store.load_remote(self.dir), {"WXCLIENT_UI_ACCOUNTS": "a"})

    def test_effective_still_falls_back_to_the_environment(self) -> None:
        settings_store.save_remote(self.dir, {"WXCLIENT_UI_ACCOUNTS": "后端给的"})

        values = settings_store.effective(self.dir, {"WXCLIENT_GATEWAY_AI_WS_URL": "wss://env/ws"})

        self.assertEqual(values["WXCLIENT_UI_ACCOUNTS"], "后端给的")
        self.assertEqual(values["WXCLIENT_GATEWAY_AI_WS_URL"], "wss://env/ws")


if __name__ == "__main__":
    unittest.main()

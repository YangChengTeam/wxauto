"""配置中心保存前的地板检查（用户 2026-09-18：「配置中心不是有对应的配置吗」）。

`读消息间隔` / `查新好友间隔` 两格在界面上能填，但网关那边有硬地板（`pacing.py`）：
填低了 `clamp_interval` 会悄悄抬回去、只打一条日志，界面上仍显示你填的数——
「配置改了没反应」。现在保存时就拒，并把地板写在说明里。

这份不依赖 tkinter，Linux 上也跑。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wxclient.ui import settings_store
from wxclient.ui.settings_store import FIELDS, validate

POLL = "WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS"
FRIEND = "WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS"


def _field(key: str) -> settings_store.Field:
    return next(f for f in FIELDS if f.key == key)


class FloorsMatchTheGatewayTests(unittest.TestCase):
    """★ 界面上写的地板必须就是网关真正用的那个数——抄错了这里判死。"""

    def test_the_two_floors_are_the_pacing_constants(self) -> None:
        from wxclient.gateway.pacing import (
            MIN_FRIEND_INTERVAL_SECONDS,
            MIN_READ_INTERVAL_SECONDS,
        )

        self.assertEqual(_field(POLL).minimum, MIN_READ_INTERVAL_SECONDS)
        self.assertEqual(_field(FRIEND).minimum, MIN_FRIEND_INTERVAL_SECONDS)

    def test_the_floor_is_written_in_the_help_text(self) -> None:
        """人得在填之前就看到那个数，而不是保存被拒了才知道。"""
        for key in (POLL, FRIEND):
            field = _field(key)
            self.assertIn(f"最低 {field.minimum:g}", field.help, field.label)

    def test_no_other_field_grew_a_floor_by_accident(self) -> None:
        """地板只给网关真会 clamp 的两格。加好友最小间隔刻意没有地板（config.py 里写明「调小自己清楚」）。"""
        floored = {f.key for f in FIELDS if f.minimum is not None and not f.section.startswith("demo")}
        self.assertEqual(floored, {POLL, FRIEND})


class ValidateTests(unittest.TestCase):
    def test_below_the_floor_is_refused_with_the_floor_in_the_sentence(self) -> None:
        problems = validate({POLL: "10"})
        self.assertEqual(set(problems), {POLL})
        self.assertIn("最低 30", problems[POLL])
        self.assertIn("10", problems[POLL])

    def test_at_or_above_the_floor_passes(self) -> None:
        self.assertEqual(validate({POLL: "30", FRIEND: "300"}), {})
        self.assertEqual(validate({POLL: "180", FRIEND: "900"}), {})

    def test_zero_is_off_for_friend_check_but_not_for_reading(self) -> None:
        self.assertEqual(validate({FRIEND: "0"}), {})
        self.assertIn(POLL, validate({POLL: "0"}))

    def test_blank_means_default_and_is_fine(self) -> None:
        self.assertEqual(validate({POLL: "", FRIEND: "   "}), {})
        self.assertEqual(validate({}), {})

    def test_garbage_is_refused_instead_of_crashing_the_gateway_later(self) -> None:
        problems = validate({POLL: "三十"})
        self.assertIn(POLL, problems)
        self.assertIn("数字", problems[POLL])

    def test_fields_without_a_floor_are_never_judged(self) -> None:
        """加好友最小间隔填 10 也放行——那格的取舍写在 config.py 里，不归这里管。"""
        self.assertEqual(validate({"WXCLIENT_GATEWAY_FRIEND_INTERVAL_SECONDS": "10"}), {})


class LoadIntoEnvironTests(unittest.TestCase):
    """★ 2026-09-22 真机：「发送总上限」填 0 存了，网关还是按老默认 30 跑。
    自动更新链把旧版环境变量传给了新版，而启动时「真实环境变量优先」——文件从来盖不过它。
    现在：文件里有的键盖过环境变量；文件没有的才回落环境变量、再回落默认。"""

    KEY = "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_file_value_overrides_a_stale_inherited_environ(self) -> None:
        settings_store.save(self.data_dir, {self.KEY: "0"})
        env = {self.KEY: "30"}
        settings_store.load_into_environ(self.data_dir, env)
        self.assertEqual(env[self.KEY], "0")

    def test_environ_still_counts_when_the_file_has_no_such_key(self) -> None:
        settings_store.save(self.data_dir, {})
        env = {self.KEY: "12"}
        settings_store.load_into_environ(self.data_dir, env)
        self.assertEqual(env[self.KEY], "12")

    def test_default_fills_in_when_neither_file_nor_environ_has_it(self) -> None:
        env: dict[str, str] = {}
        settings_store.load_into_environ(self.data_dir, env)
        default = next(f.default for f in FIELDS if f.key == self.KEY)
        self.assertEqual(env[self.KEY], default)
        self.assertEqual(default, "0", "总闸出厂默认 0 = 不限（用户 2026-09-18）")

    def test_every_field_saved_in_the_file_reaches_the_environ(self) -> None:
        """「保证所有的配置修改都能生效」（用户 2026-09-22）：不只总上限，每一格都一样。"""
        saved = {f.key: ("1" if f.kind == "bool" else "7") for f in FIELDS if not f.secret}
        # 极速开关开着会按设计把读消息间隔改成 1（`apply_demo_presets`，另有用例守），这条只看「存了就生效」。
        saved[settings_store.DEMO_TOGGLE_KEY] = "0"
        settings_store.save(self.data_dir, saved)
        env = {f.key: "stale" for f in FIELDS}
        settings_store.load_into_environ(self.data_dir, env)
        for key, value in saved.items():
            self.assertEqual(env[key], value, key)


class DemoModeTests(unittest.TestCase):
    """极速模式（用户 2026-09-30）：开着只降地板、不抖，间隔照配置中心；关着一切照旧。"""

    READ = "WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS"
    FRIEND_CHECK = "WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS"

    def _settings(self, **env: str):
        from unittest import mock

        from wxclient.gateway.config import GatewaySettings

        with mock.patch.dict("os.environ", env, clear=True):
            return GatewaySettings()

    def test_off_keeps_every_floor_and_the_jitter(self) -> None:
        from wxclient.gateway.pacing import (
            JITTER_RATIO,
            MIN_FRIEND_INTERVAL_SECONDS,
            MIN_MOMENTS_POLL_SECONDS,
            MIN_READ_INTERVAL_SECONDS,
        )

        settings = self._settings()
        self.assertFalse(settings.demo_mode)
        self.assertEqual(settings.read_floor_seconds, MIN_READ_INTERVAL_SECONDS)
        self.assertEqual(settings.friend_floor_seconds, MIN_FRIEND_INTERVAL_SECONDS)
        self.assertEqual(settings.moments_floor_seconds, MIN_MOMENTS_POLL_SECONDS)
        self.assertEqual(settings.jitter_ratio, JITTER_RATIO)
        self.assertIn(self.READ, validate({self.READ: "5"}))

    def test_on_lowers_the_floors_but_keeps_the_config_center_intervals(self) -> None:
        """★ 用户 2026-09-30「和配置中心重复了」：间隔只在配置中心填，极速模式不再盖掉它。"""
        from wxclient.gateway.config import DEMO_FRIEND_FLOOR_SECONDS, DEMO_MIN_SECONDS

        settings = self._settings(WXCLIENT_GATEWAY_DEMO_MODE="1", **{self.READ: "5"})
        self.assertEqual(settings.poll_interval_seconds, 5.0)
        self.assertEqual(settings.friend_interval_seconds, 300.0)
        self.assertEqual(settings.read_floor_seconds, DEMO_MIN_SECONDS)
        self.assertEqual(settings.friend_floor_seconds, DEMO_FRIEND_FLOOR_SECONDS, "用户 2026-10-15：极速下好友地板 10 秒")
        self.assertEqual(DEMO_FRIEND_FLOOR_SECONDS, 10.0)
        self.assertEqual(settings.moments_floor_seconds, DEMO_MIN_SECONDS)
        self.assertEqual(settings.jitter_ratio, 0.0)

    def test_on_lifts_the_total_send_cap(self) -> None:
        """用户 2026-10-07：极速模式下发送总上限自动不限；关着时填多少就是多少。"""
        total = "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR"
        self.assertEqual(self._settings(**{total: "30"}).max_sends_per_hour, 30)
        on = self._settings(WXCLIENT_GATEWAY_DEMO_MODE="1", **{total: "30"})
        self.assertEqual(on.max_sends_per_hour, 0)
        self.assertEqual(on.max_friend_actions_per_hour, 5, "加好友上限不跟着放开")

    def test_turning_on_sets_the_total_box_to_0_and_turning_off_puts_it_back(self) -> None:
        total = "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR"
        toggle = settings_store.DEMO_TOGGLE_KEY
        on = settings_store.apply_demo_presets({toggle: "1", total: "30"})
        self.assertEqual(on[total], "0")
        self.assertEqual(settings_store.apply_demo_presets({**on, toggle: "0"})[total], "30")
        # 已经开着、只备份过读消息间隔的老文件：总上限这格照样补上备份并改 0。
        old = {toggle: "1", self.READ: "5", settings_store.DEMO_SAVED_PREFIX + "POLL_INTERVAL_SECONDS": "180",
               total: "30"}
        synced = settings_store.apply_demo_presets(old)
        self.assertEqual(synced[total], "0")
        back = settings_store.apply_demo_presets({**synced, toggle: "0"})
        self.assertEqual((back[self.READ], back[total]), ("180", "30"))

    def test_save_check_follows_the_toggle(self) -> None:
        on = {"WXCLIENT_GATEWAY_DEMO_MODE": "1"}
        self.assertEqual(validate({**on, self.READ: "5"}), {})
        self.assertIn(self.READ, validate({**on, self.READ: "0.5"}))
        self.assertEqual(validate({**on, self.FRIEND_CHECK: "10"}), {}, "极速下查新好友最低 10 秒")
        self.assertIn(self.FRIEND_CHECK, validate({**on, self.FRIEND_CHECK: "5"}))
        self.assertIn(self.FRIEND_CHECK, validate({self.FRIEND_CHECK: "10"}))

    def test_turning_on_sets_the_friend_boxes_to_10_and_turning_off_puts_them_back(self) -> None:
        """用户 2026-10-15：加好友最小间隔、查好友通过间隔在极速下改成 10，关掉恢复原值。"""
        toggle = settings_store.DEMO_TOGGLE_KEY
        add = "WXCLIENT_GATEWAY_FRIEND_INTERVAL_SECONDS"
        recheck = "WXCLIENT_GATEWAY_FRIEND_RECHECK_INTERVAL_SECONDS"
        on = settings_store.apply_demo_presets({toggle: "1", add: "300", recheck: "900"})
        self.assertEqual((on[add], on[recheck]), ("10", "10"))
        back = settings_store.apply_demo_presets({**on, toggle: "0"})
        self.assertEqual((back[add], back[recheck]), ("300", "900"))

    def test_lowered_friend_check_is_restored_before_turning_demo_off(self) -> None:
        toggle = settings_store.DEMO_TOGGLE_KEY
        for original in ("900", "0"):
            on = settings_store.apply_demo_presets({toggle: "1", self.FRIEND_CHECK: original})
            self.assertEqual(on[self.FRIEND_CHECK], original, "开关不自动改查新好友间隔")
            edited = settings_store.apply_demo_presets({**on, self.FRIEND_CHECK: "10"})
            self.assertEqual(validate(edited), {})
            back = settings_store.apply_demo_presets({**edited, toggle: "0"})
            self.assertEqual(back[self.FRIEND_CHECK], original)
            self.assertEqual(validate(back), {}, "关闭极速前先恢复普通模式合法值")

    def test_ui_floor_matches_the_gateway_floor(self) -> None:
        from wxclient.gateway.config import DEMO_FRIEND_FLOOR_SECONDS, DEMO_MIN_SECONDS

        self.assertEqual(settings_store.DEMO_MIN_SECONDS, DEMO_MIN_SECONDS)
        self.assertEqual(settings_store.DEMO_FRIEND_FLOOR_SECONDS, DEMO_FRIEND_FLOOR_SECONDS)

    def test_no_interval_is_stored_twice(self) -> None:
        """极速页不许再出现配置中心已有的那几个间隔。"""
        demo = [f for f in FIELDS if f.section in settings_store.DEMO_SECTIONS]
        center_labels = {f.label for f in FIELDS if f.section in settings_store.SETTINGS_SECTIONS}
        self.assertFalse({f.label for f in demo} & center_labels)
        self.assertEqual(
            [f.key for f in demo if f.section == "demo"],
            ["WXCLIENT_GATEWAY_DEMO_MODE", "WXCLIENT_GATEWAY_DEMO_JITTER_PERCENT"],
        )

    def test_turning_on_sets_the_read_box_to_1_and_turning_off_puts_it_back(self) -> None:
        """用户 2026-09-30：「开启极速模式读消息间隔变为 1，框框中的参数也要变」。"""
        toggle = settings_store.DEMO_TOGGLE_KEY
        on = settings_store.apply_demo_presets({toggle: "1", self.READ: "120"})
        self.assertEqual(on[self.READ], "5")
        self.assertEqual(validate(on), {})
        back = settings_store.apply_demo_presets({**on, toggle: "0"})
        self.assertEqual(back[self.READ], "120")
        self.assertFalse([k for k in back if k.startswith(settings_store.DEMO_SAVED_PREFIX)])

    def test_an_old_file_with_the_toggle_already_on_still_gets_1(self) -> None:
        """★ 09-30 真机：0.1.103 存的文件开关早是开的，按「翻面」判就永远停在 180。"""
        toggle = settings_store.DEMO_TOGGLE_KEY
        old = {toggle: "1", self.READ: "180", "WXCLIENT_GATEWAY_DEMO_POLL_INTERVAL_SECONDS": "5"}
        synced = settings_store.apply_demo_presets(old)
        self.assertEqual(synced[self.READ], "5")
        self.assertNotIn("WXCLIENT_GATEWAY_DEMO_POLL_INTERVAL_SECONDS", synced)
        self.assertEqual(settings_store.apply_demo_presets({**synced, toggle: "0"})[self.READ], "180")

    def test_editing_the_box_while_on_is_kept(self) -> None:
        toggle = settings_store.DEMO_TOGGLE_KEY
        on = settings_store.apply_demo_presets({toggle: "1", self.READ: "120"})
        edited = settings_store.apply_demo_presets({**on, self.READ: "8"})
        self.assertEqual(edited[self.READ], "8")
        self.assertEqual(settings_store.apply_demo_presets({**edited, toggle: "0"})[self.READ], "120")

    def test_turning_off_without_a_backup_keeps_the_value(self) -> None:
        toggle = settings_store.DEMO_TOGGLE_KEY
        values = {toggle: "0", self.READ: "60"}
        self.assertEqual(settings_store.apply_demo_presets(values), values)

    def test_startup_fixes_the_file_on_disk(self) -> None:
        """启动灌环境变量时顺手对齐并落盘：配置中心进页重读磁盘就是 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            toggle = settings_store.DEMO_TOGGLE_KEY
            settings_store.save(Path(tmp), {toggle: "1", self.READ: "180"})
            env: dict[str, str] = {}
            settings_store.load_into_environ(Path(tmp), env)
            self.assertEqual(env[self.READ], "5")
            self.assertEqual(settings_store.load(Path(tmp))[self.READ], "5")

    def test_guard_and_jitter_follow_the_demo_floors(self) -> None:
        from wxclient.gateway import pacing
        from wxclient.gateway.config import DEMO_FRIEND_FLOOR_SECONDS, DEMO_MIN_SECONDS
        from wxclient.gateway.guard import (
            READ_ACTION,
            READ_FRIENDS_ACTION,
            read_intervals_from,
        )
        from wxclient.gateway.pacing import MIN_FRIEND_INTERVAL_SECONDS

        self.assertEqual(read_intervals_from(self._settings())[READ_FRIENDS_ACTION], MIN_FRIEND_INTERVAL_SECONDS)
        on = self._settings(WXCLIENT_GATEWAY_DEMO_MODE="1")
        self.assertEqual(read_intervals_from(on)[READ_FRIENDS_ACTION], DEMO_FRIEND_FLOOR_SECONDS)
        self.assertEqual(read_intervals_from(on)[READ_ACTION], DEMO_MIN_SECONDS)
        try:
            pacing.set_jitter_ratio(on.jitter_ratio)
            self.assertEqual(pacing.jittered(12.0), 12.0)
        finally:
            pacing.set_jitter_ratio(pacing.JITTER_RATIO)
        self.assertGreaterEqual(pacing.jittered(12.0), 12.0)

    def test_demo_sections_stay_off_the_config_center(self) -> None:
        self.assertFalse(set(settings_store.DEMO_SECTIONS) & set(settings_store.SETTINGS_SECTIONS))


class SpeedKnobTableTests(unittest.TestCase):
    """极速模式那张表（`wxclient/tuning.py`）：灰着显示的数必须就是源码里真在用的那个。"""

    def test_every_locked_value_is_the_real_hard_coded_value(self) -> None:
        import importlib
        from unittest import mock

        from wxclient import tuning
        from wxclient.gateway.config import GatewaySettings

        with mock.patch.dict("os.environ", {}, clear=True):
            gateway = GatewaySettings()
        for knob in tuning.KNOBS:
            where, attr = knob.target.split(":", 1)
            actual = getattr(gateway, attr) if where == "settings" else getattr(
                importlib.import_module(where), attr)
            self.assertEqual(float(actual), float(knob.locked), knob.name)

    def test_every_knob_has_a_field_on_the_speed_page(self) -> None:
        from wxclient import tuning

        keys = {f.key for f in FIELDS if f.section in settings_store.DEMO_SECTIONS}
        for knob in tuning.KNOBS:
            self.assertIn(knob.key, keys)
            self.assertIn(knob.section, settings_store.DEMO_SECTIONS)
        self.assertTrue(all(f.locked_value for f in FIELDS
                            if f.section in settings_store.DEMO_SECTIONS and f.kind != "bool"))

    def test_apply_on_then_off_restores_the_originals(self) -> None:
        from wxclient import tuning
        from wxclient.gateway import runner, schedule, wechat_io

        on = {tuning.TOGGLE_KEY: "1",
              "WXCLIENT_GATEWAY_DEMO_MAX_PEERS_PER_POLL": "15",
              "WXCLIENT_GATEWAY_DEMO_CONTACTS_SYNC_INTERVAL_SECONDS": "7",
              "WXCLIENT_GATEWAY_DEMO_PAUSE_SCALE": "0"}
        try:
            changed = tuning.apply(on)
            self.assertEqual(wechat_io.MAX_PEERS_PER_POLL, 15)
            self.assertEqual(runner.CONTACTS_SYNC_INTERVAL_SECONDS, 7.0)
            self.assertEqual(tuning.PAUSE_SCALE, 0.0)
            self.assertEqual(schedule.CHECK_INTERVAL_SECONDS, 5.0, "没填的用极速默认值")
            self.assertIn("MAX_PEERS_PER_POLL", changed)
        finally:
            self.assertEqual(tuning.apply({}), {})
        self.assertEqual(wechat_io.MAX_PEERS_PER_POLL, 8)
        self.assertEqual(runner.CONTACTS_SYNC_INTERVAL_SECONDS, 120.0)
        self.assertEqual(tuning.PAUSE_SCALE, 1.0)
        self.assertEqual(schedule.CHECK_INTERVAL_SECONDS, 30.0)

    def test_settings_level_knobs_follow_the_toggle(self) -> None:
        from unittest import mock

        from wxclient.gateway.config import GatewaySettings

        env = {"WXCLIENT_GATEWAY_DEMO_DEBOUNCE_SECONDS": "0",
               "WXCLIENT_GATEWAY_DEMO_TURN_TIMEOUT_SECONDS": "60"}
        with mock.patch.dict("os.environ", env, clear=True):
            off = GatewaySettings()
        with mock.patch.dict("os.environ", {**env, "WXCLIENT_GATEWAY_DEMO_MODE": "1"}, clear=True):
            on = GatewaySettings()
        self.assertEqual((off.debounce_seconds, off.turn_timeout_seconds), (3.0, 180.0))
        self.assertEqual((on.debounce_seconds, on.turn_timeout_seconds), (0.0, 60.0))

    def test_jitter_percent_is_editable_in_speed_mode(self) -> None:
        from unittest import mock

        from wxclient.gateway.config import GatewaySettings

        env = {"WXCLIENT_GATEWAY_DEMO_MODE": "1", "WXCLIENT_GATEWAY_DEMO_JITTER_PERCENT": "10"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertAlmostEqual(GatewaySettings().jitter_ratio, 0.1)

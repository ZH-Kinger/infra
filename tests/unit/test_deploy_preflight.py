"""`deploy/panel/preflight.py`：升级前在目标服务器上跑的只读检查。

这个脚本**刻意不 import delivery**：它跑的时候服务器上还是旧代码，导进来的状态清单会是
旧的（里面就有 `claimable`），那条「线上还有没有读不出来的单子」的检查就自己把自己废了。
代价是清单只能手抄 —— 手抄的东西会漂移，所以这里拿真清单去比对它。

数据全部虚构，不碰任何真实文件。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from delivery import tickets as t

#: 脚本不在包里，按路径加载
PREFLIGHT = Path(__file__).resolve().parents[2] / "deploy" / "panel" / "preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("panel_preflight", PREFLIGHT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = _load()


def check_tickets(items):
    """把一份申请单数据写成文件、跑一遍检查，返回 (problems, warnings)。输出不进测试日志。"""
    d = Path(tempfile.mkdtemp())
    path = d / "tickets.json"
    if items is not None:
        payload = items if isinstance(items, str) else json.dumps(items, ensure_ascii=False)
        path.write_text(payload, encoding="utf-8")
    r = pf.Result()
    with contextlib.redirect_stdout(io.StringIO()):
        pf.check_tickets(r, path)
    return r.problems, r.warnings


def check_env(lines):
    """把一份 EnvironmentFile 写成文件、跑一遍检查，返回 (problems, warnings)。"""
    path = Path(tempfile.mkdtemp()) / "panel.env"
    path.write_text("\n".join(lines), encoding="utf-8")
    r = pf.Result()
    with contextlib.redirect_stdout(io.StringIO()):
        pf.check_env(r, path)
    return r.problems, r.warnings


def ticket(status, **over):
    return {"id": f"REQ-{status}", "kind": "permission", "status": status, **over}


def file_of(items):
    """一份**形状正常**的申请单文件。schema 要带上：`TicketStore._read` 认死它。"""
    return {"schema": pf.SCHEMA, "tickets": items}


class StatusListDriftTests(unittest.TestCase):
    """手抄的那两份清单和真清单对不上，这个脚本给出的结论就是错的。"""

    def test_schema_string_matches_the_real_one(self):
        """又一处手抄。抄错了的话，一份**完全正常**的线上文件会被报成「降级过」而拦住部署 ——
        反过来若真清单改了版本号而这里没跟，读不出来的文件会被放行。"""
        self.assertEqual(pf.SCHEMA, t.SCHEMA)

    def test_known_status_is_exactly_the_real_state_machine(self):
        """加了或删了状态却忘了同步过来 —— 这条就是那个提醒。

        少抄一个：线上一批正常单子会被报成「这版代码不认得」，人会以为文件被改过。
        多抄一个：真正读不出来的单子被放过去，升上去才发现。
        """
        self.assertEqual(pf.KNOWN_STATUS, set(t.TRANSITIONS))
        self.assertEqual(pf.KNOWN_STATUS, set(t.LABELS), "状态、转换表、中文名三处要一致")

    def test_removed_statuses_are_not_also_in_the_known_list(self):
        """两份清单交叉了的话，一张停在已删状态的单子会被判成「认得」，直接放行。"""
        self.assertEqual(pf.KNOWN_STATUS & pf.GONE_STATUS, set())

    def test_removed_statuses_really_are_gone_from_the_code(self):
        """GONE_STATUS 里的值必须在现役代码里查无此物，否则它拦的是一个还活着的状态。"""
        for status in pf.GONE_STATUS:
            self.assertNotIn(status, t.TRANSITIONS, status)
            self.assertNotIn(status, t.LABELS, status)

    def test_the_script_does_not_import_the_package_it_checks(self):
        """一旦 import 了 delivery，手抄清单就会被旧代码的真清单顶掉，检查失去意义。

        （而且服务器上跑它的时候 `src/` 未必在 sys.path 里，import 直接就崩了。）
        """
        source = PREFLIGHT.read_text(encoding="utf-8")
        for line in source.splitlines():
            line = line.strip()
            self.assertFalse(line.startswith(("import delivery", "from delivery")), line)


class CheckEnvTests(unittest.TestCase):
    """`check_env`：EnvironmentFile 里的几处硬依赖。只看键在不在，不打印任何值。"""

    GOOD = "DELIVERY_BASE_URL=https://panel.example.com"

    def test_no_issuer_identity_is_a_warning_not_a_blocker(self):
        """只发 ≤12 小时 STS 凭证、或压根没有凭证模板的部署是**合法**的，不能拦住它们部署。

        真正按模板分级判定的是面板的 `/health`；这脚本看不到模板，只能提醒。
        """
        problems, warnings = check_env([self.GOOD])
        self.assertEqual(problems, [], problems)
        self.assertEqual(len(warnings), 1, warnings)

    def test_an_issuer_identity_passes_clean(self):
        problems, warnings = check_env(
            [self.GOOD, f"DELIVERY_ISSUER_ALIYUN_{'1' * 16}_ACCESS_KEY_ID=x"]
        )
        self.assertEqual((problems, warnings), ([], []))

    def test_an_exec_identity_without_an_issuer_gets_the_extra_hint(self):
        """两把 AK 是故意分开的。把开通身份复制成发放身份的话，云上根本不让它建号发 AK。"""
        _, warnings = check_env([self.GOOD, "DELIVERY_EXEC_ALIYUN_1_ACCESS_KEY_ID=x"])
        self.assertEqual(len(warnings), 2, warnings)

    def test_no_value_is_ever_printed(self):
        """这脚本在生产机上跑，输出会被贴进群里。"""
        out = io.StringIO()
        r = pf.Result()
        path = Path(tempfile.mkdtemp()) / "panel.env"
        path.write_text(
            "\n".join([self.GOOD, "DELIVERY_ISSUER_ALIYUN_1_ACCESS_KEY_SECRET=topsecretvalue"]),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(out):
            pf.check_env(r, path)
        self.assertNotIn("topsecretvalue", out.getvalue())
        self.assertNotIn("topsecretvalue", json.dumps(r.problems + r.warnings, ensure_ascii=False))


class CheckTicketsTests(unittest.TestCase):
    """`check_tickets`：线上已有的单子，新代码认不认得。"""

    def test_a_ticket_stuck_in_a_removed_status_blocks_the_upgrade(self):
        problems, _ = check_tickets(file_of([ticket("claimable"), ticket("done")]))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("claimable", problems[0][0])
        self.assertTrue(problems[0][1], "报了问题就得给一句怎么修")

    def test_every_removed_status_is_caught_not_just_the_first(self):
        problems, _ = check_tickets(file_of([ticket(s) for s in sorted(pf.GONE_STATUS)]))
        self.assertEqual(len(problems), 1, problems)
        for status in pf.GONE_STATUS:
            self.assertIn(status, problems[0][0], status)

    def test_a_healthy_file_passes_clean(self):
        problems, warnings = check_tickets(
            file_of([ticket("done"), ticket("rejected"), ticket("revoked")])
        )
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])

    def test_every_live_status_passes(self):
        """真状态机里的每一个值都要放行 —— 漏抄一个的症状是「一批好单子被报成坏的」。"""
        problems, _ = check_tickets(file_of([ticket(s) for s in sorted(t.TRANSITIONS)]))
        self.assertEqual(problems, [], problems)

    def test_an_unknown_status_is_reported_too(self):
        """既不在现役清单、也不在已删清单 —— 降级过或文件被改过，同样不能升。"""
        problems, _ = check_tickets(file_of([ticket("waiting_for_godot")]))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("waiting_for_godot", problems[0][0])

    def test_a_bare_list_file_is_understood_too(self):
        """有的导出是裸数组。读成「没有单子」就等于这条检查静默跳过了。"""
        problems, _ = check_tickets([ticket("claimable")])
        self.assertEqual(len(problems), 1, problems)

    def test_no_file_at_all_is_a_fresh_install_not_a_problem(self):
        problems, warnings = check_tickets(None)
        self.assertEqual((problems, warnings), ([], []))

    def test_a_broken_file_stops_the_upgrade_instead_of_being_read_as_empty(self):
        """读不出来时当成「没有单子」是最糟的：明明有一堆单子，检查却全绿。"""
        problems, _ = check_tickets("{这不是 json")
        self.assertEqual(len(problems), 1, problems)

    def test_a_wrong_schema_blocks_the_upgrade(self):
        """`TicketStore._read` 认死 schema —— 对不上的话面板一张单子都读不出来，
        而这脚本以前对这种文件报 ✓：查完全绿，升上去整个面板是空的。"""
        for label, schema in (("降级过", "wuji-tickets@0"), ("指错了文件", "wuji-people@1")):
            with self.subTest(label):
                problems, _ = check_tickets({"schema": schema, "tickets": [ticket("done")]})
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(schema, problems[0][0])
                self.assertTrue(problems[0][1], "报了问题就得给一句怎么修")

    def test_a_missing_schema_blocks_the_upgrade(self):
        """半路手写、或被别的脚本重新生成过的文件常常就少这一行。"""
        problems, _ = check_tickets({"tickets": [ticket("done")]})
        self.assertEqual(len(problems), 1, problems)

    def test_the_schema_check_stops_before_reading_statuses(self):
        """schema 不对就别再顺着往下报状态：一份指错了的文件会刷出一堆看不懂的状态问题，
        真正的那一句（「你指错文件了」）被埋在中间。"""
        problems, _ = check_tickets({"schema": "nope@1", "tickets": [ticket("claimable")]})
        self.assertEqual(len(problems), 1, problems)
        self.assertNotIn("claimable", problems[0][0])

    def test_tickets_field_of_the_wrong_type_blocks_the_upgrade(self):
        """不是数组的话面板读出来是空的。当成「没有单子」放行是最糟的那种绿。"""
        for label, value in (("对象", {"REQ-1": {}}), ("字符串", "[]"), ("null", None)):
            with self.subTest(label):
                problems, _ = check_tickets(file_of(value))
                self.assertEqual(len(problems), 1, problems)

    def test_elements_that_are_not_objects_block_the_upgrade(self):
        """数组里混进裸值 —— 后面每一处 `item.get(...)` 都会炸。"""
        problems, _ = check_tickets(file_of([ticket("done"), "REQ-2"]))
        self.assertEqual(len(problems), 1, problems)

    def test_undelivered_credentials_are_a_warning_not_a_blocker(self):
        """失败/已关闭但还留着子账号的凭证单：升上去第一轮定时任务就会删掉它们。

        先报个数让人知道会发生什么，但不该拦住部署 —— 那些凭证本来就谁都用不了。
        """
        items = [
            ticket("failed", kind="credential", cred_user="tempak-a-1"),
            ticket("closed", kind="credential", cred_user="tempak-b-2"),
            ticket("failed", kind="credential"),  # 没建出号，不算
            ticket("done", kind="credential", cred_user="tempak-c-3"),  # 正常在用，不算
            ticket("failed"),  # 不是凭证单
        ]
        problems, warnings = check_tickets(file_of(items))
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("2", warnings[0][0])
        # 只报数量，不把子账号名打出来
        for name in ("tempak-a-1", "tempak-b-2", "tempak-c-3"):
            self.assertNotIn(name, warnings[0][0], name)


if __name__ == "__main__":
    unittest.main()

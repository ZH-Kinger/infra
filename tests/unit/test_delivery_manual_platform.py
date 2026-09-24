"""九章 / TurboAI（曦望）：没有接口的平台，面板只派活、不建号。

这两个平台**没有任何 API**，号只能管理员在各自控制台手工开。所以这条链上面板能做的
只有：申请 → 飞书审批 → 通知管理员去开 → 管理员建完回填 → 进人工登记名单 → 单子才算完。

这个文件锁的是「每一个出口说的是同一件事」。最怕的失败不是报错，是**报成功但其实没做**：
阿里和火山的开账号单批了就是开好了，人已经习惯这件事；这两个平台批了之后什么都没发生，
任何一个出口漏说一句，申请人就会去等一个永远不会来的账号。

数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import assets as assets_mod
from delivery import catalog as catalog_mod
from delivery import health, requests_api
from delivery import notify as notify_mod
from delivery import offline_accounts as offline_mod
from delivery import people as people_mod
from delivery import platforms as platforms_mod
from delivery import tickets as t
from delivery import todo as todo_mod
from delivery.approval import Applicant, ApprovalConfig, ApprovalError, FeishuApproval
from delivery.errors import DeliveryError
from delivery.flows import FlowError, Flows, _approval_fields, is_trouble
from delivery.provision import ProvisionError

from . import test_delivery_health as health_test
from . import test_delivery_todo_api as todo_api
from .test_delivery_access_requests import FakeFeishu

#: 阿里云主账号（对照组：它有接口，行为一个字都不该变）
ACC = "1000000000000001"
#: 九章的租户标识。**不是数字云账号 ID** —— 那个平台自己就这么标识租户
JZ = "wuji"
#: 曦望的租户标识：一个邮箱。同样不是数字
TA = "Wuji-Algorithm@wuji.tech"
#: 飞书后台手工加的选项只能拿到自动生成的 ID，长这样
JZ_OPTION = "muf5nlly-7ba4-4f5c-9d3a-000000000001"
TA_OPTION = "muf5nlly-7ba4-4f5c-9d3a-000000000002"

TEMPLATES = {
    "schema": catalog_mod.SCHEMA,
    "templates": [
        {
            "id": "jiuzhang-new-user",
            "kind": "account",
            "platform": "jiuzhang",
            "account": JZ,
            "title": "九章子账号",
        },
        {
            "id": "turboai-new-user",
            "kind": "account",
            "platform": "turboai",
            "account": TA,
            "title": "TurboAI 子账号",
        },
        # 对照组：有接口的平台，面板自己建号。这条模板的行为一个字都不该变
        {
            "id": "aliyun-new-user",
            "kind": "account",
            "platform": "aliyun",
            "account": ACC,
            "title": "新员工子账号",
            "groups": ["grp-default"],
            "console_login": True,
        },
        # 对照组之二：资源开通。「回填」这个动作本来是它的，开账号那条分支
        # 不能把它接管了
        {
            "id": "rds-free",
            "kind": "resource",
            "platform": "aliyun",
            "account": ACC,
            "title": "RDS 实例",
            "max_days": 0,
        },
    ],
}

CONFIG = ApprovalConfig(
    approval_code="APPROVAL-1",
    # **`account` 这一栏必须配上控件**，否则 `_approval_fields` 算出来的值会被
    # `FeishuApproval.create` 静默跳过 —— 那样对照表这条路在用例里根本走不到
    widgets={
        "ticket_id": "w1",
        "kind": "w2",
        "summary": "w3",
        "reason": "w4",
        "account": "w5",
        "cloud_user": "w6",
    },
    comment_open_id="ou_panel_bot",
    account_options={f"jiuzhang/{JZ}": JZ_OPTION, f"turboai/{TA}": TA_OPTION},
)

LI = Applicant(union_id="on_li", name="李四", open_id="ou_li")
#: 名册里没有阿里云账号的人 —— 九章的登录名算不出来
NEW = Applicant(union_id="on_new", name="新人", open_id="ou_new")
#: 只有火山的号：阿里这边照样算「还没有子账号」，用来测占名那条
WANG = Applicant(union_id="on_wang", name="王五", open_id="ou_wang")


def _roster():
    """名册。**登录名里的点要被去掉**：九章 18 个号 18/18 是 `wuji-<阿里登录名去点>`。"""
    return people_mod.parse(
        {
            "schema": people_mod.SCHEMA,
            "people": [
                {
                    "name": "李四",
                    "email": "li.si@wuji.tech",
                    "union_id": "on_li",
                    "accounts": [{"platform": "aliyun", "account": ACC, "name": "li.si"}],
                },
                {"name": "新人", "email": "new@wuji.tech", "union_id": "on_new", "accounts": []},
                {
                    "name": "王五",
                    "email": "wang.wu@wuji.tech",
                    "union_id": "on_wang",
                    # 只有火山的号：九章的登录名是按**阿里云**登录名推的，推不出来
                    "accounts": [
                        {"platform": "volcano", "account": "2000000001", "name": "wangwu"}
                    ],
                },
            ],
        }
    )


OFFLINE = {
    "accounts": [
        {
            "platform": "jiuzhang",
            "account": JZ,
            "source": "九章控制台导出",
            "as_of": "2026-09-22",
            "login_prefix": "wuji-",
            "users": [{"name": "wuji-laowang", "email": "lao.wang@wuji.tech", "status": "正常"}],
        },
        {
            "platform": "turboai",
            "account": TA,
            "source": "曦望控制台名单",
            "as_of": "2026-09-01",
            "users": [{"name": "zhangwt", "email": "zhang.wentao@wuji.tech", "status": "正常"}],
        },
    ]
}


class Env:
    """一整条链：模板 + 审批 + 名册 + 人工登记表 + 派活通知。

    执行器工厂**被调到就抛** —— 线上它正是这么失败的：这两个平台没有凭证，
    构造执行器那一步就找不到东西。人工平台这条路一个云调用都不该发，
    连执行器都不该构造出来。
    """

    def __init__(self, *, announce="default", register="default", templates=TEMPLATES):
        self.dir = Path(tempfile.mkdtemp())
        self.templates = json.loads(json.dumps(templates))
        self.feishu = FakeFeishu()
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        self.approval = FeishuApproval(CONFIG, lambda: "tenant-token", transport=self.feishu)
        #: 执行器工厂被调用的次数。**人工平台这一路必须是空的**
        self.executor_calls = []
        #: 派活卡：(单号, 面板算出来的登录名)
        self.cards = []
        #: 下一次派活要报告的失败（返回值里的「发不出去的原因」）
        self.announce_problems = []
        #: 非 None 时派活直接抛这个异常
        self.announce_raises = None
        self.offline = self.dir / "offline-accounts.json"
        self.offline.write_text(json.dumps(OFFLINE, ensure_ascii=False), encoding="utf-8")
        self.offline.chmod(0o600)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: self.approval,
            approvals=lambda name: self.approval if not name else None,
            roster=_roster,
            executor=self._executor,
            announce_manual=self._announce if announce == "default" else announce,
            register_manual=self._register if register == "default" else register,
            clock=lambda: self.now[0],
        )

    def _executor(self, platform, account):
        self.executor_calls.append((platform, account))
        raise ProvisionError(f"没有 {platform}/{account} 的凭证")

    def _announce(self, ticket, login=""):
        self.cards.append((ticket.get("id"), login))
        if self.announce_raises is not None:
            raise self.announce_raises
        return list(self.announce_problems)

    def _register(self, platform, account, user, actor, ticket_id):
        offline_mod.merge_one(
            str(self.offline),
            platform=platform,
            account=account,
            user=user,
            actor=actor,
            ticket_id=ticket_id,
        )

    # ── 流程快捷方式 ──────────────────────────────────────────────

    def submit(
        self,
        template="jiuzhang-new-user",
        who=LI,
        username="wuji-lisi",
        email="li.si@wuji.tech",
    ):
        return self.flows.submit(
            applicant=who,
            email=email,
            template_id=template,
            payload={"username": username},
            reason="要在九章上跑训练任务",
        )

    def approve(self, ticket):
        self.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        return self.flows.sync(ticket["id"], force=True)

    def run(
        self,
        template="jiuzhang-new-user",
        who=LI,
        username="wuji-lisi",
        email="li.si@wuji.tech",
    ):
        return self.approve(self.submit(template, who, username, email))

    def registry(self, platform="jiuzhang"):
        data = json.loads(self.offline.read_text(encoding="utf-8"))
        return next(a for a in data["accounts"] if a["platform"] == platform)

    def events(self, ticket_id):
        return [e.get("event") for e in self.store.get(ticket_id).get("events") or ()]

    def notes(self, ticket_id):
        return [str(e.get("note") or "") for e in self.store.get(ticket_id).get("events") or ()]


class PlatformRegistryTests(unittest.TestCase):
    """`platforms.MANUAL` 从 `{id: 名字}` 变成了 `{id: ManualPlatform}`。

    **派生关系不能因为换了容器就断掉**：`MANUAL_IDS`、`NAMES`、`offboard.MANUAL_PLATFORMS`
    以前都从那张表推出来，现在值变成了对象，写 `v.name` 的地方漏一处就是显示名变成
    `ManualPlatform(id='jiuzhang', …)` 贴在卡片上。
    """

    def test_manual_returns_the_profile_and_none_for_everything_else(self):
        self.assertEqual(platforms_mod.manual("jiuzhang").name, "九章")
        self.assertEqual(platforms_mod.manual("jiuzhang").login_prefix, "wuji-")
        # 曦望那 5 个号是三种写法，没有规则 —— 留空，让管理员回填
        self.assertEqual(platforms_mod.manual("turboai").login_prefix, "")
        for other in ("aliyun", "volcano", "internal", "", "JIUZHANG", None):
            with self.subTest(platform=other):
                self.assertIsNone(platforms_mod.manual(other))

    def test_names_still_derive_from_the_one_table(self):
        """显示名从 `MANUAL[x].name` 推出来，不是第二份字面量。

        写死 `"jiuzhang": "九章"` 在**今天**也成立 —— 所以按内容比，不按存在比。
        """
        for pid, spot in platforms_mod.MANUAL.items():
            with self.subTest(platform=pid):
                self.assertIsInstance(spot, platforms_mod.ManualPlatform)
                self.assertEqual(spot.id, pid, "表的键要和档案里的 id 一致")
                self.assertEqual(platforms_mod.NAMES[pid], spot.name)
                self.assertEqual(platforms_mod.name_of(pid), spot.name)
                self.assertNotIn("ManualPlatform", platforms_mod.NAMES[pid])
        self.assertEqual(platforms_mod.MANUAL_IDS, tuple(platforms_mod.MANUAL))
        # 换了容器之后这条还得成立：人工平台不是真·云平台，凡按 IDS 遍历云的地方
        # 都不该看到它（遍历到的表现是去调一个不存在的接口）
        for pid in platforms_mod.MANUAL_IDS:
            self.assertNotIn(pid, platforms_mod.IDS)

    def test_the_profile_is_frozen(self):
        """档案是只读的：一处改了 `login_prefix`，别处算出来的登录名就跟着变。"""
        with self.assertRaises(dataclasses.FrozenInstanceError):
            platforms_mod.MANUAL["jiuzhang"].login_prefix = "x-"


class CatalogManualTests(unittest.TestCase):
    """人工平台的模板：只放行开账号，且拒绝一切「面板会去做」的字段。"""

    def _parse(self, **over):
        data = json.loads(json.dumps(TEMPLATES))
        spec = data["templates"][0]
        spec.update(over)
        return catalog_mod.parse(data)

    def test_account_template_loads_with_a_non_numeric_tenant(self):
        cat = self._parse()
        self.assertEqual(cat.get("jiuzhang-new-user").account, JZ)
        self.assertEqual(cat.get("turboai-new-user").account, TA)

    def test_other_kinds_are_rejected(self):
        """面板在这两个平台上除了「登记一张开号单」什么都做不了。

        放行别的类型的表现是：单子提交得出去，审批也过，然后在开通那一步炸 ——
        而申请人看到的是一次失败，他会以为重试能好。
        """
        for kind in ("permission", "credential", "resource", "storage"):
            with self.subTest(kind=kind):
                with self.assertRaises(catalog_mod.CatalogError) as ctx:
                    self._parse(kind=kind)
                self.assertIn("九章", str(ctx.exception))

    def test_fields_the_panel_would_have_to_execute_are_rejected(self):
        """`groups` / `workspaces` / `console_login` 配了也不会生效，所以拒绝加载。

        静默忽略的表现是：有人照着阿里那条模板配了 `groups`，以为新号会自动进组，
        而面板压根不调任何接口 —— 他要等到有人抱怨「权限没给」才会发现。
        """
        for field, value in (
            ("groups", ["grp-default"]),
            ("console_login", True),
            ("workspaces", [{"region": "cn-hangzhou", "workspace": "ws", "roles": ["user"]}]),
        ):
            with self.subTest(field=field):
                with self.assertRaises(catalog_mod.CatalogError) as ctx:
                    self._parse(**{field: value})
                self.assertIn(field, str(ctx.exception))

    def test_every_useless_field_is_named_at_once(self):
        """一次报全，不是修一个冒一个 —— 配模板的人只会改报出来的那一个。"""
        with self.assertRaises(catalog_mod.CatalogError) as ctx:
            self._parse(groups=["g"], console_login=True)
        self.assertIn("groups", str(ctx.exception))
        self.assertIn("console_login", str(ctx.exception))

    def test_tenant_id_must_stay_usable_as_a_registry_key(self):
        """人工登记表的键是 `平台/账号/登录名`，账号里带 `/` 两边就对不上了 ——
        而症状是「号开完了、面板里还显示可以申请」，没有任何地方会报错。"""
        with self.assertRaises(catalog_mod.CatalogError):
            self._parse(account="wuji/prod")
        with self.assertRaises(catalog_mod.CatalogError):
            self._parse(account="w" * 129)
        # 128 字符正好放行
        self.assertEqual(self._parse(account="w" * 128).get("jiuzhang-new-user").account, "w" * 128)

    def test_real_clouds_still_demand_a_numeric_account(self):
        """对照组：放开「账号不必是数字」不能顺手把阿里那条也放开。"""
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"][2]["account"] = "wuji"
        with self.assertRaises(catalog_mod.CatalogError) as ctx:
            catalog_mod.parse(data)
        self.assertIn("数字", str(ctx.exception))

    def test_unknown_platform_message_lists_the_manual_ones(self):
        with self.assertRaises(catalog_mod.CatalogError) as ctx:
            self._parse(platform="nosuchcloud")
        for pid in platforms_mod.MANUAL_IDS:
            self.assertIn(pid, str(ctx.exception))

    def test_awaits_human_is_true_for_manual_platforms(self):
        """**这条是「已开通」那句谎话的闸门。**

        `awaits_human` 说 False 的话，单子批完直接落 DONE —— 台账上写着「已开通」，
        而那个号根本不存在。六个出口（状态、文案、卡片、待办、体检、通知）都读它。
        """
        cat = self._parse()
        for tid in ("jiuzhang-new-user", "turboai-new-user"):
            with self.subTest(template=tid):
                self.assertTrue(catalog_mod.awaits_human(cat.get(tid)))
        # 对照组：阿里的开账号单面板自己建，不等人
        self.assertFalse(catalog_mod.awaits_human(cat.get("aliyun-new-user")))

    def test_awaits_human_follows_the_manual_list_at_runtime(self):
        """变异：名单里多一个平台，判定要跟着走（写死平台名的话这里不会变）。"""

        class Tpl:
            kind = "account"
            platform = "newcloud"
            resource_type = ""

        self.assertFalse(catalog_mod.awaits_human(Tpl()))
        from unittest import mock

        with mock.patch.object(platforms_mod, "MANUAL_IDS", ("jiuzhang", "newcloud")):
            self.assertTrue(catalog_mod.awaits_human(Tpl()))


class ApprovalOptionTableTests(unittest.TestCase):
    """「云账号」那一栏的选项对照表。配错了单子会落「提交失败」，且报错和云账号无关。"""

    def _load(self, options):
        path = Path(tempfile.mkdtemp()) / "approval.json"
        data = {
            "approval_code": "APPROVAL-1",
            "widgets": {k: f"w-{k}" for k in ("ticket_id", "kind", "summary", "reason")},
        }
        if options is not None:
            data["account_options"] = options
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ApprovalConfig.load(str(path))

    def test_absent_means_empty_and_changes_nothing(self):
        self.assertEqual(dict(self._load(None).account_options), {})
        self.assertEqual(dict(ApprovalConfig("c", {}).account_options), {})

    def test_values_are_trimmed(self):
        got = self._load({"  jiuzhang/wuji  ": "  opt-1  "}).account_options
        self.assertEqual(dict(got), {"jiuzhang/wuji": "opt-1"})

    def test_bad_shapes_are_refused_at_load_time(self):
        """**留到提交那一刻才炸的话，报错和「云账号」毫无关系** —— 现场是一张
        「提交失败」的单子和一句飞书的表单校验错误，没人会想到是这张表配错了。"""
        # JSON 的键一定是字符串，所以这里只能覆盖到「值不对」和「空键」那几种；
        # 「键不是 str」那条走不到文件这条路（dict 直接构造时才可能）
        for bad in (
            ["jiuzhang/wuji"],
            "jiuzhang/wuji",
            {"jiuzhang/wuji": 123},
            {"jiuzhang/wuji": None},
            {"jiuzhang/wuji": ["opt-1"]},
            {"jiuzhang/wuji": ""},
            {"jiuzhang/wuji": "   "},
            {"": "opt-1"},
            {"   ": "opt-1"},
        ):
            with self.subTest(options=bad):
                with self.assertRaises(ApprovalError) as ctx:
                    self._load(bad)
                self.assertIn("account_options", str(ctx.exception))

    def test_empty_table_is_allowed(self):
        self.assertEqual(dict(self._load({}).account_options), {})


class ApprovalFieldTests(unittest.TestCase):
    """送审批的「云账号」值。对不上选项 → 飞书拒掉**整张表单**。"""

    def test_manual_platforms_send_the_feishu_option_id(self):
        got = _approval_fields(
            {"kind": "account", "platform": "jiuzhang", "account": JZ},
            {"username": "wuji-lisi"},
            CONFIG.account_options,
        )
        self.assertEqual(got["account"], JZ_OPTION)
        self.assertEqual(got["cloud_user"], "wuji-lisi")

    def test_real_clouds_are_untouched(self):
        """阿里和火山的行为**逐字不变**：它们的选项值当初是用 API 指定的，就是 `平台/账号`。"""
        got = _approval_fields(
            {"kind": "account", "platform": "aliyun", "account": ACC},
            {"username": "lisi"},
            CONFIG.account_options,
        )
        self.assertEqual(got["account"], f"aliyun/{ACC}")

    def test_without_a_table_everything_falls_back_to_platform_slash_account(self):
        for options in (None, {}, {"other/1": "opt"}):
            with self.subTest(options=options):
                got = _approval_fields(
                    {"kind": "account", "platform": "jiuzhang", "account": JZ},
                    {"username": "wuji-lisi"},
                    options,
                )
                self.assertEqual(got["account"], f"jiuzhang/{JZ}")

    def test_the_table_applies_to_every_kind_that_sends_an_account(self):
        """对照表在 `acct` 算出来的**那一处**替换，不是按类型各写一遍。"""
        for kind, payload in (
            ("permission", {"cloud_user": "x"}),
            ("resource", {"spec": "8 卡"}),
            ("transfer", {"source": "oss://a/", "dest": "tos://b/"}),
        ):
            with self.subTest(kind=kind):
                got = _approval_fields(
                    {"kind": kind, "platform": "jiuzhang", "account": JZ},
                    payload,
                    CONFIG.account_options,
                )
                self.assertEqual(got["account"], JZ_OPTION)

    def test_the_submitted_form_really_carries_the_option_id(self):
        """端到端：飞书真正收到的那张表单里，云账号那一栏是选项 ID。

        只断 `_approval_fields` 的话，「算对了但没接上」这种漏法测不出来。
        """
        env = Env()
        ticket = env.submit()
        form = json.loads(env.feishu.instances[ticket["approval"]["instance_code"]]["form"])
        values = {f["id"]: f["value"] for f in form}
        self.assertEqual(values["w5"], JZ_OPTION)
        # 只有「云账号」那一栏换成选项 ID。摘要里出现 `jiuzhang/wuji` 是给人读的，
        # 不参与选项匹配 —— 换掉它反而会让审批人看到一串没有意义的 ID
        self.assertNotEqual(values["w5"], f"jiuzhang/{JZ}")

    def test_a_real_cloud_submits_the_plain_platform_slash_account(self):
        """对照组：阿里那一栏逐字不变。对照表只该改表里有的那几条。"""
        env = Env()
        ticket = env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        form = json.loads(env.feishu.instances[ticket["approval"]["instance_code"]]["form"])
        values = {f["id"]: f["value"] for f in form}
        self.assertEqual(values["w5"], f"aliyun/{ACC}")


class ManualLoginTests(unittest.TestCase):
    """面板猜的登录名。**算不出来就留空，绝不硬猜** —— 猜错的后果是管理员照着建，
    云上多一个没人用的号，而且那个号不在任何人名下。"""

    def login(self, env, tpl_id, union_id):
        tpl = env.flows._catalog().get(tpl_id)  # noqa: SLF001 — 直接测这层算法
        return env.flows._manual_login(tpl, {"applicant": {"union_id": union_id}})  # noqa: SLF001

    def test_jiuzhang_strips_the_dots_from_the_aliyun_login(self):
        self.assertEqual(self.login(Env(), "jiuzhang-new-user", "on_li"), "wuji-lisi")

    def test_turboai_says_it_cannot_tell(self):
        """曦望那 5 个号是三种写法（名姓倒置 / 姓名顺序 / 缩写），没有规则。"""
        self.assertEqual(self.login(Env(), "turboai-new-user", "on_li"), "")

    def test_missing_pieces_return_empty_without_raising(self):
        """名册坏了不该让一张单失败 —— 少的只是卡片上的一句提示。"""
        env = Env()
        for union_id, why in (
            ("", "登录态里没有 union_id"),
            ("on_nobody", "名册里没这个人"),
            ("on_new", "这个人没有阿里云账号"),
            ("on_wang", "只有火山的号，推不出阿里登录名"),
        ):
            with self.subTest(why=why):
                self.assertEqual(self.login(env, "jiuzhang-new-user", union_id), "")

    def test_a_broken_roster_does_not_raise(self):
        env = Env()

        def boom():
            raise OSError("名册文件读不了")

        env.flows._roster = boom  # noqa: SLF001
        self.assertEqual(self.login(env, "jiuzhang-new-user", "on_li"), "")

    def test_a_broken_roster_does_not_fail_the_ticket(self):
        """整条链走一遍：名册坏了，单子照样停在「待开通」，卡片照发（只是没有登录名）。"""
        env = Env()
        ticket = env.submit()

        def boom():
            raise OSError("名册文件读不了")

        env.flows._roster = boom  # noqa: SLF001
        done = env.approve(ticket)
        self.assertEqual(done["status"], t.FULFILLING)
        self.assertEqual(env.cards, [(done["id"], "")])


class ManualExecuteTests(unittest.TestCase):
    """审批通过那一刻：面板什么都没做，而且每个出口都这么说。"""

    def test_no_cloud_call_and_no_executor_is_even_built(self):
        """**人工平台的分支必须排在 `ex = self._executor(...)` 之前。**

        排在后面的话，构造执行器那一步就去找这个平台的凭证 —— 它根本没有凭证，
        于是整张单在「审批已通过」之后炸成 FAILED。申请人看到的是一次失败，
        而这张单其实只是需要管理员去那边点几下。
        """
        env = Env()
        done = env.run()
        self.assertEqual(env.executor_calls, [], "人工平台这一路连执行器都不该构造")
        self.assertEqual(done["status"], t.FULFILLING)

    def test_it_stops_at_awaiting_fulfilment_not_done(self):
        env = Env()
        done = env.run()
        self.assertEqual(done["status"], t.FULFILLING)
        self.assertIn("await_fulfil", env.events(done["id"]))
        # 「完成时间」不能现在就写：那个号还不存在
        self.assertFalse(done.get("done_at_ts"))
        self.assertFalse(done.get("user_created"))
        self.assertFalse(done.get("manual_created"))

    def test_the_ledger_never_says_the_account_exists(self):
        env = Env()
        done = env.run()
        result = str(done.get("result") or "")
        for lie in ("已新建", "已开通", "已建好"):
            self.assertNotIn(lie, result)
        self.assertIn("面板没有建号", result)
        self.assertIn("九章", result)
        self.assertIn("wuji-lisi", result, "算得出登录名就写进去，管理员照着建")

    def test_turboai_result_does_not_invent_a_login_name(self):
        env = Env()
        done = env.run(template="turboai-new-user", username="zhangwt")
        result = str(done.get("result") or "")
        self.assertIn("面板没有建号", result)
        self.assertIn("TurboAI", result)
        self.assertNotIn("登录名 ", result, "算不出来就别写一个出来")

    def test_the_admin_gets_a_card_with_the_login_name(self):
        env = Env()
        done = env.run()
        self.assertEqual(env.cards, [(done["id"], "wuji-lisi")])

    def test_the_card_goes_out_after_the_ticket_is_really_in_fulfilling(self):
        """派活卡排在状态落盘**之后**：先发卡的话，写状态万一失败，
        管理员收到「去开号」却找不到可回填的单子。"""
        env = Env()
        seen = []
        env.flows._announce_manual = lambda ticket, login="": (  # noqa: SLF001
            seen.append(env.store.get(ticket["id"])["status"]) or []
        )
        env.run()
        self.assertEqual(seen, [t.FULFILLING])

    def test_real_clouds_still_go_through_the_executor(self):
        """对照组：阿里的开账号单照旧走执行器（这里的执行器会抛，所以落 FAILED）。

        少了这条，「把人工分支写成对所有平台生效」也能让上面那些用例全绿。
        """
        env = Env()
        ticket = env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        done = env.approve(ticket)
        self.assertEqual(env.executor_calls, [("aliyun", ACC)])
        self.assertEqual(done["status"], t.FAILED)
        self.assertEqual(env.cards, [], "有接口的平台不该给管理员派活")


class AdminNoticeTests(unittest.TestCase):
    """派活通知发不出去：单子状态不变，但**必须留痕且命中问题词**。

    少了问题词，sweep 会把这一轮当成干净的一轮退 0 —— 管理员既没收到通知、
    也没收到告警，单子就这么静静躺着。
    """

    def _assert_complained(self, env, ticket_id):
        self.assertEqual(self.store_status(env, ticket_id), t.FULFILLING)
        self.assertIn("manual_notice_failed", env.events(ticket_id))
        self.assertNotIn("manual_notice_sent", env.events(ticket_id))
        bad = [n for n in env.notes(ticket_id) if is_trouble(n)]
        self.assertTrue(bad, f"没有一行命中 TROUBLE_WORDS：{env.notes(ticket_id)}")

    @staticmethod
    def store_status(env, ticket_id):
        return env.store.get(ticket_id)["status"]

    def test_not_wired_up_is_not_a_silent_downgrade(self):
        """**`announce_manual` 没接上那条分支不能删。**

        漏接和发失败是同一个后果（没有人被通知到），所以走同一条留痕路径。
        删掉它的话，漏接会变成「一切正常」—— 而这正是最难发现的那种失败。
        """
        env = Env(announce=None)
        done = env.run()
        self.assertEqual(done["status"], t.FULFILLING)
        self._assert_complained(env, done["id"])
        self.assertIn("通知", " ".join(env.notes(done["id"])))

    def test_partial_failure_is_reported(self):
        env = Env()
        env.announce_problems = ["on_admin…：TimeoutError: 连不上飞书"]
        done = env.run()
        self._assert_complained(env, done["id"])
        self.assertIn("TimeoutError", " ".join(env.notes(done["id"])))

    def test_an_exception_does_not_roll_back_an_approved_ticket(self):
        """通知炸了不该把一张已经批过的单子判失败 —— 审批是真的过了。"""
        env = Env()
        env.announce_raises = RuntimeError("飞书 token 过期")
        done = env.run()
        self.assertEqual(done["status"], t.FULFILLING)
        self._assert_complained(env, done["id"])

    def test_success_leaves_a_quiet_event(self):
        env = Env()
        done = env.run()
        self.assertIn("manual_notice_sent", env.events(done["id"]))
        self.assertEqual([n for n in env.notes(done["id"]) if is_trouble(n)], [])

    def test_the_ticket_can_still_be_fulfilled_after_a_failed_notice(self):
        """通知没发出去只是没人知道，单子本身没坏：管理员自己发现了照样能回填。"""
        env = Env(announce=None)
        done = env.run()
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(filled["status"], t.DONE)


class ManualFulfilTests(unittest.TestCase):
    """回填：管理员在那边建完号，回来填登录名。这一步才把单子做完。"""

    def test_it_lands_in_the_registry_and_only_then_says_done(self):
        env = Env()
        done = env.run()
        before = env.registry()
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        after = env.registry()
        self.assertEqual(filled["status"], t.DONE)
        self.assertEqual(
            [u["name"] for u in after["users"]], ["wuji-laowang", "wuji-lisi"], "加人，不是替换"
        )
        added = after["users"][-1]
        self.assertEqual(added["display_name"], "李四")
        self.assertEqual(added["email"], "li.si@wuji.tech")
        self.assertEqual(added["status"], "正常")
        self.assertEqual(before["as_of"], after["as_of"], "加一个人不等于整份名单重新核对过")

    def test_it_marks_manual_created_not_user_created(self):
        """**`user_created` 是「面板自己建的号」的标记。**

        写上它的话，定时任务 `retry_iam_writes` 每天都会拿这张单去给账号补写公司 IAM，
        而 `iam_sync` 跳过非 IDS 平台 —— 于是每天一条永远修不好的假故障。
        """
        env = Env()
        done = env.run()
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertTrue(filled["manual_created"])
        self.assertEqual(filled["manual_login"], "wuji-lisi")
        self.assertFalse(filled.get("user_created"))
        # 行为层面的那一半：定时任务不该捞到它
        self.assertEqual(env.flows.retry_iam_writes(), [])

    def test_turboai_accepts_whatever_the_admin_actually_created(self):
        env = Env()
        done = env.run(template="turboai-new-user", username="zhangsan3")
        filled = env.flows.fulfil(done["id"], actor="admin", note="zhenyuan")
        self.assertEqual(filled["status"], t.DONE)
        self.assertEqual(
            [u["name"] for u in env.registry("turboai")["users"]], ["zhangwt", "zhenyuan"]
        )

    def test_a_wrong_looking_login_is_refused_and_the_ticket_stays_put(self):
        """填错的后果是名册里多一个对不上任何人的号，而**没有任何地方会报**。"""
        env = Env()
        done = env.run()
        for bad, why in (
            ("laowang", "没有 wuji- 前缀"),
            ("WUJI-LISI", "大写"),
            ("", "空"),
            ("   ", "只有空格"),
            ("wuji lisi", "带空格"),
            ("wuji-lisi!", "带标点"),
        ):
            with self.subTest(why=why), self.assertRaises(FlowError):
                env.flows.fulfil(done["id"], actor="admin", note=bad)
        self.assertEqual(env.store.get(done["id"])["status"], t.FULFILLING)
        self.assertEqual([u["name"] for u in env.registry()["users"]], ["wuji-laowang"])

    def test_a_registry_write_failure_keeps_the_ticket_open(self):
        """**写不进名单就不推 DONE。**

        名册、「我的账号」、离职检查全靠那份名单；只把登录名写进单子的话，
        这个号在面板眼里不存在，人离职时没有任何提示。
        """

        def boom(*_a, **_kw):
            raise offline_mod.OfflineError("登记表锁住了")

        env = Env(register=boom)
        done = env.run()
        with self.assertRaises(offline_mod.OfflineError):
            env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(env.store.get(done["id"])["status"], t.FULFILLING)
        self.assertNotIn("fulfilled", env.events(done["id"]))

    def test_no_registry_wired_up_is_an_explicit_error(self):
        env = Env(register=None)
        done = env.run()
        with self.assertRaises(FlowError) as ctx:
            env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(env.store.get(done["id"])["status"], t.FULFILLING)

    def test_fulfilling_twice_is_refused(self):
        """**第二次要用一个不同的名字。** 同名的话 `merge_one` 幂等提前返回，
        这条用例就只是在测幂等，测不到「先校验状态、再动名册」那个顺序。

        顺序反了的现场：管理员填错名字、重填一次 —— 第二个名字已经写进名册，
        状态更新才报「这张单已完成」。他看到报错会以为什么都没写，而名册里多了一个
        云上根本不存在的号，还带着申请人的邮箱进快照、进名册，
        离职检查会派人去停一个不存在的号。
        """
        env = Env()
        done = env.run()
        env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        with self.assertRaises(FlowError) as ctx:
            env.flows.fulfil(done["id"], actor="admin", note="wuji-dierci")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(
            [u["name"] for u in env.registry()["users"]], ["wuji-laowang", "wuji-lisi"]
        )

    def test_a_ticket_that_is_not_awaiting_fulfilment_never_touches_the_registry(self):
        """同一条的另一半：刚批过还在开通中的单子、已关的单子，都不能写名册。"""
        env = Env()
        done = env.run()
        env.store.update(
            done["id"], actor="admin", expect=[t.FULFILLING], to=t.CLOSED, event="closed"
        )
        with self.assertRaises(FlowError) as ctx:
            env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual([u["name"] for u in env.registry()["users"]], ["wuji-laowang"])

    def test_the_registry_email_comes_from_the_roster_not_the_form(self):
        """**名册里那个号靠邮箱匹配到人。**

        用申请人自报的邮箱（提交表单里那个）的话，写错一个字这个号就永远匹配不到人 ——
        于是**永远不进离职检查**，而那正是登记这件事要堵的洞。
        """
        env = Env()
        # 提交时自报的邮箱打错了（或填了个人邮箱）。名册里那份才是准的
        done = env.run(email="li.si@gmail.com")
        self.assertEqual(done["applicant"]["email"], "li.si@gmail.com", "单子上记的就是自报那个")
        env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        added = env.registry()["users"][-1]
        self.assertEqual(added["email"], "li.si@wuji.tech")

    def test_no_email_anywhere_refuses_the_fulfil(self):
        """一个匹配不到人的号进了名单，比不进更糟：它看上去被管着，其实没人认领。"""
        env = Env()
        done = env.run(who=NEW, username="wuji-xinren", email="")
        # 名册里这个人也没有邮箱
        env.flows._roster = lambda: people_mod.parse(  # noqa: SLF001
            {
                "schema": people_mod.SCHEMA,
                "people": [{"name": "新人", "email": "", "union_id": "on_new", "accounts": []}],
            }
        )
        with self.assertRaises(FlowError) as ctx:
            env.flows.fulfil(done["id"], actor="admin", note="wuji-xinren")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual([u["name"] for u in env.registry()["users"]], ["wuji-laowang"])

    def test_a_real_cloud_account_ticket_cannot_be_fulfilled_by_hand(self):
        """对照组：阿里的开账号单没有「回填」这回事 —— 面板自己建的号。"""
        env = Env()
        ticket = env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        env.approve(ticket)
        with self.assertRaises(FlowError) as ctx:
            env.flows.fulfil(ticket["id"], actor="admin", note="xinren")
        self.assertEqual(ctx.exception.status, 409)

    def test_resource_tickets_still_take_their_own_path(self):
        """回填这条路本来是资源开通的。开账号那条分支排在最前面，不能把资源单接管了。

        接管的表现：登记实例 ID 时被当成登录名校验（`i-bp1xxxx` 过不了人工平台那道
        前缀门），管理员登记不进去，而报错说的是「登录名不合规矩」。
        """
        env = Env()
        ticket = env.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id="rds-free",
            payload={"spec": "4 核 8G"},
            reason="要一台测试库跑回归",
        )
        done = env.approve(ticket)
        self.assertEqual(done["status"], t.FULFILLING)
        filled = env.flows.fulfil(
            done["id"], actor="admin", note="已开通", resource_ids=["rm-bp1xxxx"]
        )
        self.assertEqual(filled["status"], t.DONE)
        self.assertFalse(filled.get("manual_created"), "资源单不该被记成人工平台开号")
        self.assertEqual([u["name"] for u in env.registry()["users"]], ["wuji-laowang"])


class TicketActionTests(unittest.TestCase):
    """面板上那个「登记开通结果」按钮。"""

    ADMIN = requests_api.Caller("on_adm", "管理员", "adm@wuji.tech", "ou_adm", "u_adm", True)
    USER = requests_api.Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "u_li", False)

    def test_admin_sees_fulfil_on_a_manual_account_ticket(self):
        env = Env()
        done = env.run()
        self.assertTrue(requests_api.ticket_view(done, viewer=self.ADMIN)["actions"]["fulfil"])
        self.assertFalse(requests_api.ticket_view(done, viewer=self.USER)["actions"]["fulfil"])

    def test_the_button_disappears_once_it_is_done(self):
        env = Env()
        done = env.run()
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertFalse(requests_api.ticket_view(filled, viewer=self.ADMIN)["actions"]["fulfil"])


class ManualCardTests(unittest.TestCase):
    """派活卡。这是这条链上**唯一会主动找人**的东西。"""

    TICKET = {
        "id": "REQ-42",
        "kind": "account",
        "template": {"id": "jiuzhang-new-user", "platform": "jiuzhang", "account": JZ},
        "applicant": {"name": "李四", "email": "li.si@wuji.tech", "union_id": "on_li"},
        # **申请理由在 ticket 顶层**（`flows.submit` 写的），不在 payload 里。
        # 而人工平台的 payload **按设计恒为 `{}`**（`_validate` 的人工分支）——
        # 所以「从 payload 取 reason」不是「偶尔取不到」，是**永远取不到**：
        # 管理员收到的卡上永远没有「为什么要这个号」，而卡上别的字段都在，看不出少了东西
        "reason": "要在九章上跑训练",
        "payload": {},
    }

    def text(self, **kw):
        return json.dumps(notify_mod.manual_account_card(self.TICKET, **kw), ensure_ascii=False)

    def test_it_says_who_where_and_what_name(self):
        body = self.text(login="wuji-lisi")
        for must in ("李四", "li.si@wuji.tech", "九章", JZ, "wuji-lisi", "REQ-42"):
            self.assertIn(must, body)
        self.assertIn("要在九章上跑训练", body)

    def test_the_reason_comes_from_the_ticket_not_the_payload(self):
        """取 `payload["reason"]` 的话这一行**永远**渲染不出来（payload 恒为 `{}`）。

        管理员照着卡片去建号，看不到「为什么要这个号」—— 而卡上别的字段都在，
        少一行没人看得出来。
        """
        got = notify_mod.manual_account_card(
            {**self.TICKET, "payload": {"reason": "这是塞在 payload 里的假理由"}},
            login="wuji-lisi",
        )
        body = json.dumps(got, ensure_ascii=False)
        self.assertIn("要在九章上跑训练", body)
        self.assertNotIn("假理由", body, "payload 里的 reason 不该被当真")

    def test_it_admits_when_the_login_cannot_be_derived(self):
        """**不编一个。** 管理员会照着建，于是云上多一个没人用的号。"""
        card = notify_mod.manual_account_card(
            {**self.TICKET, "template": {"platform": "turboai", "account": TA}}
        )
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("算不出来", body)
        self.assertIn("TurboAI", body)

    def test_it_says_the_panel_did_not_create_anything(self):
        body = self.text(login="wuji-lisi")
        self.assertIn("面板开不了", body)
        self.assertIn("待开通", body)
        for lie in ("已新建", "已开通"):
            self.assertNotIn(lie, body)

    def test_the_link_only_appears_when_there_is_a_base_url(self):
        self.assertIn("https://panel.example.com", self.text(base_url="https://panel.example.com/"))
        self.assertNotIn("http", self.text().replace("https://open.feishu.cn", ""))

    def test_a_ticket_missing_pieces_still_renders(self):
        """卡片是通知路径上的最后一环，它自己不能成为「发不出去」的原因。"""
        card = notify_mod.manual_account_card({"template": {"platform": "jiuzhang"}})
        self.assertIn("九章", json.dumps(card, ensure_ascii=False))


class MergeOneTests(unittest.TestCase):
    """往人工登记名单里加一个人。**`as_of` 一个字不改。**"""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "offline-accounts.json"
        self.path.write_text(json.dumps(OFFLINE, ensure_ascii=False), encoding="utf-8")
        self.path.chmod(0o600)

    def raw(self, platform="jiuzhang"):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return next(a for a in data["accounts"] if a["platform"] == platform)

    def add(self, name="wuji-lisi", platform="jiuzhang", account=JZ, **kw):
        return offline_mod.merge_one(
            str(self.path),
            platform=platform,
            account=account,
            user={"name": name, "email": "li.si@wuji.tech", "status": "正常", **kw},
            actor="admin",
            ticket_id="REQ-42",
        )

    def test_it_adds_without_touching_anything_else(self):
        before = json.loads(self.path.read_text(encoding="utf-8"))
        self.add()
        after = self.raw()
        self.assertEqual([u["name"] for u in after["users"]], ["wuji-laowang", "wuji-lisi"])
        self.assertEqual(after["as_of"], "2026-09-22")
        self.assertEqual(after["source"], "九章控制台导出")
        self.assertEqual(after["login_prefix"], "wuji-")
        # 别的账号一个字不动
        self.assertEqual(
            self.raw("turboai"), next(a for a in before["accounts"] if a["platform"] == "turboai")
        )

    def test_as_of_is_not_refreshed(self):
        """**`as_of` 的意思是「这份名单截至哪天是完整的」。**

        加一个人不代表其余人重新核对过。刷成今天就是在说谎 —— 而体检页正是靠它
        提醒「这份名单旧了，可能漏报」，一刷就再也不会提醒了。
        """
        for name in ("wuji-a", "wuji-b", "wuji-c"):
            self.add(name)
        self.assertEqual(self.raw()["as_of"], "2026-09-22")
        self.assertEqual(self.raw("turboai")["as_of"], "2026-09-01")

    def test_adding_the_same_person_twice_is_a_no_op(self):
        """回填重试安全：飞书按钮点两下、sweep 和面板同时补一次，都不该多一条。"""
        self.add()
        self.add()
        self.assertEqual([u["name"] for u in self.raw()["users"]].count("wuji-lisi"), 1)
        self.assertEqual(len(self.raw()["history"]), 1, "重复回填不该再记一笔")

    def test_an_unregistered_account_is_an_explicit_error(self):
        """名单里还没有这个账号 → 明确报错，不凭空建一条。

        凭空建的话那条记录没有 `as_of`、没有 `source`，体检页无从判断它有多旧，
        而它实际上**只有面板开过的那一个号** —— 一份看上去很新、其实漏掉全部存量的名单。
        """
        with self.assertRaises(offline_mod.OfflineError) as ctx:
            self.add(platform="jiuzhang", account="another-tenant")
        self.assertIn("another-tenant", str(ctx.exception))
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["accounts"]), 2)

    def test_an_empty_login_is_refused(self):
        for bad in ({"name": ""}, {"name": "   "}, {}):
            with self.subTest(user=bad), self.assertRaises(offline_mod.OfflineError):
                offline_mod.merge_one(
                    str(self.path),
                    platform="jiuzhang",
                    account=JZ,
                    user=bad,
                    actor="admin",
                )

    def test_a_name_that_breaks_the_table_rules_never_reaches_disk(self):
        """写前整表 parse：不合规的条目不会落盘，原文件一个字不变。"""
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(offline_mod.OfflineError):
            self.add(name="laowang")  # 缺 wuji- 前缀，登记表自己也拒
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_unknown_user_fields_are_refused(self):
        """最常见的是顺手把手机号粘进来 —— 名册用不上它，多存一份就多一份泄漏面。"""
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(offline_mod.OfflineError):
            self.add(phone="13800000000")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_history_records_who_and_which_ticket(self):
        self.add()
        entry = self.raw()["history"][-1]
        self.assertEqual((entry["added"], entry["removed"], entry["changed"]), (1, 0, 0))
        self.assertEqual(entry["by"], "admin")
        self.assertEqual(entry["ticket"], "REQ-42")
        self.assertEqual(entry["total"], 2)

    def test_history_is_trimmed(self):
        for i in range(offline_mod.HISTORY_KEEP + 5):
            self.add(name=f"wuji-u{i}")
        self.assertEqual(len(self.raw()["history"]), offline_mod.HISTORY_KEEP)

    def test_the_file_stays_owner_only(self):
        """名单里有全公司的企业邮箱。写一次就把权限放宽的话，没人会发现。"""
        self.add()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_it_never_replaces_the_whole_list(self):
        """和 `save_account` 的分水岭：来源是「刚建了一个号」，不是「粘了一份完整名单」。

        整体替换的话，回填一次就把名单里其余人抹掉 —— 那些人在离职检查里凭空消失。
        """
        self.add()
        self.assertIn("wuji-laowang", [u["name"] for u in self.raw()["users"]])

    def test_a_missing_file_is_still_an_error_not_a_fresh_list(self):
        missing = str(Path(tempfile.mkdtemp()) / "offline-accounts.json")
        with self.assertRaises(offline_mod.OfflineError):
            offline_mod.merge_one(
                missing,
                platform="jiuzhang",
                account=JZ,
                user={"name": "wuji-lisi"},
                actor="admin",
            )

    def test_concurrent_adds_do_not_lose_anyone(self):
        """读-改-写必须在锁里。**面板和 sweep 是两个进程**，都会往这份名单里加人。

        丢更新的表现是名册里少一个号：那个人离职时没有任何提示，而云上的号还开着 ——
        和一开始不登记是同一个后果。
        """
        import threading

        names = [f"wuji-u{i:02d}" for i in range(12)]
        start = threading.Barrier(len(names))
        errors = []

        def add(name):
            start.wait()
            try:
                self.add(name)
            except Exception as exc:  # noqa: BLE001 — 收集起来一起断言
                errors.append(f"{name}: {exc}")

        threads = [threading.Thread(target=add, args=(n,)) for n in names]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        got = [u["name"] for u in self.raw()["users"]]
        self.assertEqual(sorted(got), sorted(["wuji-laowang", *names]))
        self.assertEqual(self.raw()["as_of"], "2026-09-22")


class WiringTests(unittest.TestCase):
    """两条路（面板 / sweep）都得把这两个可选依赖接上。

    **漏接的症状不一样，但都坏**：`announce_manual` 漏接 = 没人被通知（单子安静躺着），
    `register_manual` 漏接 = 回填时 503。单测一律直接构造 `Flows`，测不到
    「参数是怎么传到它手上的」—— 所以这里直接读源码里那处调用。
    """

    DEPS = ("announce_manual", "register_manual")

    def kwargs(self, path, inside):
        from .test_delivery_serve_wiring import REPO, call_in

        named, _spread, _node = call_in(REPO / path, "Flows", inside=inside)
        return set(named)

    def test_the_panel_wires_both(self):
        missing = sorted(set(self.DEPS) - self.kwargs("src/delivery/server.py", "flows"))
        self.assertEqual(missing, [], f"server.py 构造 Flows 时漏了 {missing}")

    def test_the_sweep_wires_both(self):
        missing = sorted(set(self.DEPS) - self.kwargs("src/delivery/cli_requests.py", "_sweep"))
        self.assertEqual(missing, [], f"cli_requests._sweep 构造 Flows 时漏了 {missing}")

    def test_flows_really_accepts_them(self):
        """签名对不上的话，上面两条读到的名字只是「写了但没人收」—— 启动即 TypeError。"""
        import inspect

        params = inspect.signature(Flows.__init__).parameters
        for dep in self.DEPS:
            self.assertIn(dep, params)
            self.assertIsNone(params[dep].default, f"{dep} 有默认值才不会逼所有用例改签名")


class TemplateDriftTests(unittest.TestCase):
    """审批之后模板被动过。**回填按单子自己记的快照走，不按当下的模板目录。**

    这个仓库别处对「审批期间模板被改」是有纪律的（`_verify` 逐字核对 `_EXEC_FIELDS`），
    回填这一步一度漏了 —— 两条各自的症状见下面两个用例。
    """

    def test_the_control_case_works(self):
        """对照：模板没动的时候回填是通的。

        没有这条的话，下面两条说不清是「守住了」还是「本来就没跑起来」。
        """
        env = Env()
        done = env.run()
        self.assertEqual(
            env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")["status"], t.DONE
        )

    def test_removing_the_template_must_not_strand_an_approved_ticket(self):
        """模板被改名 / 下架之后，这张已经批过的单子还得能回填。

        取当下模板目录的话：`fulfil` 抛 409「只有资源开通申请需要登记开通结果」，
        单子永远停在「待开通」。而此时号**已经在九章上建好了** —— 它进不了人工登记名单，
        于是名册、「我的账号」、离职检查全都看不到它：一个开着的号，没人知道它属于谁。
        报错文案也指不到问题（说的是「资源开通申请」）。
        """
        env = Env()
        done = env.run()
        env.templates["templates"] = [
            s for s in env.templates["templates"] if s["id"] != "jiuzhang-new-user"
        ]
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(filled["status"], t.DONE)
        self.assertIn("wuji-lisi", [u["name"] for u in env.registry()["users"]])

    def test_retargeting_the_template_must_not_move_the_new_account(self):
        """审批之后有人把模板指到另一个租户 → 号**不能**被登记到那个租户名下。

        取当下模板目录的话，单子的模板快照里写着 `wuji`，名单里那个号却挂在
        `other-tenant` 下，状态还是「已完成」—— 没有任何地方报错。
        """
        env = Env()
        done = env.run()
        data = json.loads(env.offline.read_text(encoding="utf-8"))
        data["accounts"].append(
            {
                "platform": "jiuzhang",
                "account": "other-tenant",
                "source": "另一个租户",
                "as_of": "2026-09-01",
                "login_prefix": "wuji-",
                "users": [],
            }
        )
        env.offline.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for spec in env.templates["templates"]:
            if spec["id"] == "jiuzhang-new-user":
                spec["account"] = "other-tenant"
        env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        after = json.loads(env.offline.read_text(encoding="utf-8"))["accounts"]
        landed = {a["account"]: [u["name"] for u in a["users"]] for a in after}
        self.assertIn("wuji-lisi", landed[JZ], "要落在单子批的那个租户下")
        self.assertEqual(landed["other-tenant"], [], "不能落到审批之后才换上的那个租户")


class NoUsernameTests(unittest.TestCase):
    """人工平台的开号单**不收登录名**。

    收了就一定会在某处被当真：审批单的「申请内容」会写「新建子账号 lisi」，
    而真实登录名是 `wuji-lisi`（九章）或管理员随手起的（曦望）。申请人拿 lisi 登不进去，
    管理员照着审批单还可能真去建一个 lisi —— 九章 18/18 都带 `wuji-` 前缀，建错了还得删。

    这一轮把台账、结果文案、卡片、横幅、下一步提示五处都改口了，
    **而这一段是审批人真正读的那段**。改五处漏一处，效果是运维学会不信这些文案。
    """

    def summary_of(self, env, ticket):
        form = json.loads(env.feishu.instances[ticket["approval"]["instance_code"]]["form"])
        return next(f["value"] for f in form if f["id"] == "w3")

    def test_the_approval_summary_does_not_promise_a_new_subaccount(self):
        env = Env()
        said = self.summary_of(env, env.submit())
        self.assertNotIn("新建子账号", said)
        # catalog 已经禁了人工平台配 groups，「默认用户组 无」是恒成立的噪音
        self.assertNotIn("默认用户组", said)
        self.assertIn("九章", said)
        self.assertIn("面板开不了", said)
        self.assertIn("回填", said)

    def test_a_username_in_the_payload_is_ignored_not_recorded(self):
        """**不收也不留痕。** 留在 payload 里的话，下一个读它的人会以为那是真名字。"""
        env = Env()
        ticket = env.submit(username="wuji-lisi")
        self.assertEqual(ticket["payload"], {})
        self.assertNotIn("wuji-lisi", self.summary_of(env, ticket))

    def test_a_malformed_username_no_longer_blocks_the_request(self):
        """既然不用它，就不该拿它挡人 —— 何况页面上早就不该有这一栏了。"""
        env = Env()
        ticket = env.submit(username="这不是合法用户名！")
        self.assertEqual(ticket["payload"], {})
        self.assertEqual(env.approve(ticket)["status"], t.FULFILLING)

    def test_the_whole_chain_still_works_with_no_username_at_all(self):
        """端到端：一个字的用户名都没有，照样能批、能派活、能回填。"""
        env = Env()
        ticket = env.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id="jiuzhang-new-user",
            payload={},
            reason="要在九章上跑训练任务",
        )
        done = env.approve(ticket)
        self.assertEqual(done["status"], t.FULFILLING)
        self.assertEqual(env.cards, [(done["id"], "wuji-lisi")], "登录名仍然按名册算给管理员看")
        filled = env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(filled["status"], t.DONE)

    def test_a_second_request_for_the_same_platform_is_refused(self):
        """**不按用户名判重，按「谁 + 哪个平台」判。**

        面板根本不知道那个平台上已经有哪些名字，拿用户名去判会凭空误拦；
        而完全不判的话，同一个人能对同一个平台攒一堆重复的开号单，管理员挨个去建。
        """
        env = Env()
        env.submit()
        with self.assertRaises(FlowError) as ctx:
            env.submit(username="完全不同的名字")
        self.assertEqual(ctx.exception.status, 409)
        # 换个平台就放行（判重是按平台+账号的，不是「你有过一张开号单就不给了」）
        self.assertTrue(env.submit(template="turboai-new-user"))

    def test_a_second_request_after_the_first_one_finished_is_refused_too(self):
        """**这条才测得到开账号那道门。**

        上一条其实是被 `submit` 里那道通用去重（「已有一张相同的申请还没结束」）接住的：
        不收 username 之后两张单的 payload 都是 `{}`，长得一模一样。
        通用那道只看 `status in OPEN` —— 第一张单**做完之后**再提一张，它放行，
        只有开账号这道门认 DONE。号已经开出来了还能再提一张，管理员就得再建一个。
        """
        env = Env()
        done = env.run()
        env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        self.assertEqual(env.store.get(done["id"])["status"], t.DONE)
        with self.assertRaises(FlowError) as ctx:
            env.submit(username="完全不同的名字")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("九章", str(ctx.exception))

    def test_someone_who_already_has_an_account_there_is_told_where(self):
        env = Env()
        env.flows.my_accounts = lambda uid: [  # noqa: ARG005
            {"platform": "jiuzhang", "account": JZ, "name": "wuji-lisi"}
        ]
        with self.assertRaises(FlowError) as ctx:
            env.submit()
        self.assertIn("九章", str(ctx.exception))

    # ── 对照组：有接口的云那条路**一个字都没动**（它是真往云上写的） ────────

    def test_real_clouds_still_reject_a_malformed_username(self):
        """放松了就是真事故：那条路会拿这个名字去云上建号。"""
        env = Env()
        for bad in ("Xin Ren", "XINREN", "-xinren", "这不是合法用户名"):
            with self.subTest(username=bad):
                with self.assertRaises(FlowError) as ctx:
                    env.submit(template="aliyun-new-user", who=NEW, username=bad)
                self.assertIn("用户名", str(ctx.exception))

    def test_real_clouds_still_record_the_username(self):
        env = Env()
        ticket = env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        self.assertEqual(ticket["payload"], {"username": "xinren"})
        said = self.summary_of(env, ticket)
        self.assertIn("新建子账号 xinren", said)
        self.assertIn("默认用户组", said)

    def test_real_clouds_still_block_a_username_taken_by_another_request(self):
        """占名检查只对真云有意义（面板自己建的号才会重名）。这条别被顺手删掉。"""
        env = Env()
        env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        with self.assertRaises(FlowError) as ctx:
            env.submit(template="aliyun-new-user", who=WANG, username="xinren")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("已经被别的申请占用", str(ctx.exception))


class HealthOptionTableTests(unittest.TestCase):
    """体检页：人工平台模板漏配审批选项对照。

    **漏配的症状是那个平台的第一个申请人一提交就失败**，而飞书回的是一句表单校验错误，
    和「云账号」毫无关系 —— 没人会想到去翻 `approval.json`。这条纯离线、零误报，
    所以该在上线前就报出来，而不是等第一个人踩。
    """

    KEY = ("登录与审批", "飞书审批")

    def backend(self, *, options=None, templates=None, catalog_error=None):
        data = json.loads(json.dumps(templates if templates is not None else TEMPLATES))
        kw = {}
        if catalog_error is not None:
            kw["_catalog"] = catalog_error
        else:
            kw["_catalog"] = catalog_mod.parse(data)
        backend = health_test.FakeBackend(**kw)
        conf = json.loads(Path(backend.approval_path).read_text(encoding="utf-8"))
        if options is not None:
            conf["account_options"] = options
        Path(backend.approval_path).write_text(json.dumps(conf), encoding="utf-8")
        return backend

    def check(self, backend):
        return health_test.run(backend, health_test.with_base())[1][self.KEY]

    def test_missing_entries_are_crit_and_named(self):
        got = self.check(self.backend())
        self.assertEqual(got["level"], health.CRIT)
        self.assertIn(f"jiuzhang/{JZ}", got["detail"])
        self.assertIn(f"turboai/{TA}", got["detail"])
        # 修法要能照着做
        self.assertIn("account_options", got["fix"])

    def test_only_the_missing_one_is_named(self):
        """点名要准：报一串「都缺」会让人把配好的那条也重配一遍。"""
        got = self.check(self.backend(options={f"jiuzhang/{JZ}": JZ_OPTION}))
        self.assertEqual(got["level"], health.CRIT)
        self.assertNotIn(f"jiuzhang/{JZ}", got["detail"])
        self.assertIn(f"turboai/{TA}", got["detail"])

    def test_fully_configured_is_not_flagged(self):
        got = self.check(
            self.backend(options={f"jiuzhang/{JZ}": JZ_OPTION, f"turboai/{TA}": TA_OPTION})
        )
        self.assertEqual(got["level"], health.OK)

    def test_extra_unused_entries_are_not_flagged(self):
        """多配无害（下架一条模板、对照表没跟着删是常态）。报它是纯噪音，
        而噪音会教会人忽略体检页。"""
        got = self.check(
            self.backend(
                options={
                    f"jiuzhang/{JZ}": JZ_OPTION,
                    f"turboai/{TA}": TA_OPTION,
                    "jiuzhang/retired-tenant": "muf5nlly-old",
                    f"aliyun/{ACC}": "不该有但也不该报",
                }
            )
        )
        self.assertEqual(got["level"], health.OK)

    def test_a_deployment_without_manual_templates_is_not_flagged(self):
        """一个人工平台模板都没有的部署，缺这张表理所应当。"""
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"] = [
            s for s in data["templates"] if s["platform"] not in platforms_mod.MANUAL_IDS
        ]
        self.assertEqual(self.check(self.backend(templates=data))["level"], health.OK)

    def test_unreadable_templates_do_not_crash_this_check(self):
        """模板读不了是另一条的事。这一条不能因此崩掉 —— 体检页崩了等于面板崩了。"""
        backend = self.backend(catalog_error=DeliveryError("模板 x：groups 必须是数组"))
        with mock.patch("sys.stderr"):
            got = self.check(backend)
        self.assertNotIn("account_options", got.get("detail", ""))

    def test_it_follows_the_one_manual_list(self):
        """变异：名单里多一个平台，体检立刻开始要它的对照项。"""
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"][0]["platform"] = "aliyun"
        data["templates"][0]["account"] = ACC
        data["templates"][0]["id"] = "not-manual-anymore"
        del data["templates"][2]
        backend = self.backend(templates=data, options={f"turboai/{TA}": TA_OPTION})
        self.assertEqual(self.check(backend)["level"], health.OK)
        with mock.patch.object(platforms_mod, "MANUAL_IDS", ("jiuzhang", "turboai", "aliyun")):
            self.assertEqual(self.check(backend)["level"], health.CRIT)


class ManualTodoTests(todo_api.Base):
    """待办页那条「有开号单等你去建号」。

    **派活卡不够。** 卡片会被漏看（刷过去了、当天没看飞书、换了手机），
    而漏看之后就再没有任何东西提醒 —— 单子会一直躺在「待开通」，申请人以为在走流程。
    待办页是管理员的落地页，这一条是卡片之外的第二道。
    """

    def manual_ticket(self, **over):
        row = {
            "id": "REQ-1",
            "kind": "account",
            "status": t.FULFILLING,
            "created_at": "2026-09-20T10:00:00+08:00",
            "applicant": {"union_id": "on_L", "email": "l@wuji.tech", "name": "李四"},
            "template": {"id": "jiuzhang-new-user", "platform": "jiuzhang", "account": JZ},
            "events": [],
        }
        row.update(over)
        return row

    def test_a_pending_manual_account_shows_up_as_urgent(self):
        self.tickets(self.manual_ticket())
        item = self.item("request_manual_account")
        self.assertEqual(item["group"], todo_mod.URGENT)
        self.assertIn("九章", item["title"])
        # `?state=…` 前端不认（只认 open/attention/closed/all），落到「全部」等于没筛 ——
        # 管理员点「去回填」会看到一整页历史单子，得自己在里面找哪几张是待开通的
        self.assertIn("?attention", item["href"])
        self.assertNotIn("state=", item["href"])

    def test_it_is_listed_even_when_the_card_went_out_fine(self):
        """**通知成功也列出来。** 「发出去了」不等于「有人看见了」——
        按「通知失败才提醒」的话，最常见的那种失败（卡片被漏看）一条都捞不到。
        """
        sent = [{"event": "manual_notice_sent", "at": "", "actor": "system"}]
        self.tickets(self.manual_ticket(events=sent))
        self.assertIn("request_manual_account", self.kinds())

    def test_several_are_counted_in_one_row(self):
        self.tickets(
            self.manual_ticket(),
            self.manual_ticket(
                id="REQ-2",
                template={"id": "turboai-new-user", "platform": "turboai", "account": TA},
            ),
        )
        item = self.item("request_manual_account")
        self.assertEqual(item["count"], 2)
        self.assertIn("九章", item["title"])
        self.assertIn("TurboAI", item["title"])

    def test_a_real_cloud_resource_ticket_is_not_in_this_row(self):
        """对照组：资源开通单也停在「待开通」，但那条路面板本来就不建东西，
        它有自己的提醒（或者本来就不该在这一栏）。混进来的话这一栏会被噪音淹掉。"""
        self.tickets(
            self.manual_ticket(
                kind="resource",
                template={"id": "rds-free", "platform": "aliyun", "account": ACC},
            )
        )
        self.assertNotIn("request_manual_account", self.kinds())

    def test_a_finished_manual_ticket_drops_off(self):
        self.tickets(self.manual_ticket(status=t.DONE, manual_created=True, manual_login="wuji-x"))
        self.assertNotIn("request_manual_account", self.kinds())

    def test_it_follows_the_one_manual_list(self):
        """变异：名单里多一个平台，待办页立刻跟着捞它。写死平台名的话这里不会变。"""
        from unittest import mock

        self.tickets(
            self.manual_ticket(template={"id": "x", "platform": "newcloud", "account": "n"})
        )
        self.assertNotIn("request_manual_account", self.kinds())
        with mock.patch.object(platforms_mod, "MANUAL_IDS", ("jiuzhang", "turboai", "newcloud")):
            self.assertIn("request_manual_account", self.kinds())

    def test_the_manual_ticket_never_reaches_the_cloud_executor(self):
        """待办页零云调用。这条 harness 的执行器被碰到就抛 —— 新增的这一栏也得守住。"""
        self.tickets(self.manual_ticket())
        self.assertEqual(self.view()["errors"], [])


class ApplicantCardTests(unittest.TestCase):
    """开完之后发给**申请人**的那张卡。"""

    def how(self, platform, console_login=False):
        return notify_mod._account_how(  # noqa: SLF001
            {"platform": platform, "account": JZ, "console_login": console_login}
        )

    def test_it_does_not_send_people_looking_for_access_keys(self):
        """**人工平台落不到「只能用访问凭证」那句。**

        catalog 拒绝它们配 `console_login`，于是快照里恒为假 —— 而九章和曦望
        根本不发访问凭证，控制台就是唯一入口。照那句话做的人会去找一把不存在的钥匙。
        """
        for platform in platforms_mod.MANUAL_IDS:
            with self.subTest(platform=platform):
                said = self.how(platform)
                self.assertNotIn("访问凭证", said)
                self.assertIn(platforms_mod.name_of(platform), said)

    def test_real_clouds_keep_their_wording(self):
        self.assertIn("访问凭证", self.how("aliyun"))
        self.assertNotIn("访问凭证", self.how("aliyun", console_login=True))


class FrontendManualListTests(unittest.TestCase):
    """前端第三处写死的人工平台名单。

    `iam.js` 的 `BY_HAND`、`core.js` 的显示名已经各有一条守卫（见
    `test_delivery_service_offboard.ManualPlatformSourceTests`）。`requests.js` 又添了一处：
    **漏一个平台的症状是它的开号单拿到资源开通那版抽屉**（「实例 ID」「开通了什么」），
    管理员照着填必被拒成 400，而这是整条链上唯一一个人类动作。
    """

    def test_requests_js_knows_every_manual_platform(self):
        web = Path(__file__).resolve().parents[2] / "src" / "delivery" / "web"
        text = (web / "requests.js").read_text(encoding="utf-8")
        line = text.split("const MANUAL_PLATFORMS")[1].split("\n")[0]
        for pid in platforms_mod.MANUAL_IDS:
            with self.subTest(platform=pid):
                self.assertIn(
                    f'"{pid}"',
                    line,
                    f"requests.js 的 MANUAL_PLATFORMS 少了 {pid}："
                    "它的开号单会拿到资源开通那版抽屉，管理员填什么都会被拒",
                )


class HoldingsTests(unittest.TestCase):
    """资产页的「通过面板拿到的」。

    **申请人不读时间线，他看的就是这一行。** 九章/曦望的单会在「待开通」停几小时到几天 ——
    在 `awaits_human` 对人工平台返 True 之前，开账号单根本到不了 FULFILLING，
    所以这一段是这一轮新长出来的：那几天里这一行会说他已经有号了，而云上什么都没有。
    """

    @staticmethod
    def view(*tickets):
        return assets_mod.holdings_view(tickets, labels=lambda p, a: f"{p}/{a}")

    def ticket(self, **over):
        row = {
            "id": "REQ-1",
            "kind": "account",
            "status": t.FULFILLING,
            "template": {"id": "jiuzhang-new-user", "platform": "jiuzhang", "account": JZ},
            "payload": {},
        }
        row.update(over)
        return row

    def detail(self, **over):
        rows = self.view(self.ticket(**over))
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["detail"]

    def test_a_pending_manual_account_does_not_claim_he_already_has_one(self):
        said = self.detail()
        self.assertIn("九章", said)
        self.assertIn("待", said)
        # 这一页的标题是「通过面板拿到的」。说「子账号 …」就是说号已经在那儿了
        self.assertNotIn("子账号", said)

    def test_after_the_fill_in_it_shows_the_real_login(self):
        """回填写的是 `manual_login`（payload 恒为空）。不读它的话这一行光秃秃地写着
        「子账号」，没有名字 —— 他想登录都不知道自己叫什么。"""
        said = self.detail(status=t.DONE, manual_created=True, manual_login="wuji-lisi")
        self.assertIn("wuji-lisi", said)
        self.assertNotIn("待", said)

    def test_turboai_too(self):
        said = self.detail(
            template={"id": "turboai-new-user", "platform": "turboai", "account": TA}
        )
        self.assertIn("TurboAI", said)
        self.assertNotIn("子账号", said)

    def test_a_real_cloud_account_is_unchanged(self):
        """对照：阿里的开账号单面板自己建，批完就是 DONE，这一行照旧写用户名。"""
        said = self.detail(
            status=t.DONE,
            template={"id": "aliyun-new-user", "platform": "aliyun", "account": ACC},
            payload={"username": "lisi"},
        )
        self.assertEqual(said, "子账号 lisi")

    def test_a_resource_ticket_keeps_its_own_wording(self):
        """对照：资源单的「待开通」本来就有自己那句，别被人工平台那句顶掉。"""
        rows = self.view(
            {
                "id": "REQ-2",
                "kind": "resource",
                "status": t.FULFILLING,
                "template": {"id": "rds-free", "platform": "aliyun", "account": ACC},
            }
        )
        self.assertEqual(rows[0]["detail"], "待管理员登记")

    def test_it_follows_the_one_manual_list(self):
        """人工平台的名字从 `platforms.NAMES` 来，不是写死的。"""
        said = self.detail()
        self.assertIn(platforms_mod.name_of("jiuzhang"), said)


class OptionNoteTests(unittest.TestCase):
    """申请页上「你已经有了」那句提示。"""

    def test_a_manual_account_shows_the_login_that_was_filled_in(self):
        """不读 `manual_login` 的话这句会渲染成「子账号␣␣已开通」—— 中间两个空格，
        看起来像页面坏了，而真正的问题是面板不知道他的号叫什么。"""
        env = Env()
        done = env.run()
        env.flows.fulfil(done["id"], actor="admin", note="wuji-lisi")
        note = {o["id"]: o for o in env.flows.options("on_li")}["jiuzhang-new-user"]["state_note"]
        self.assertIn("wuji-lisi", note)
        self.assertNotIn("  ", note, "名字取不到就会留下两个空格")

    def test_a_real_cloud_account_note_is_unchanged(self):
        env = Env()
        ticket = env.submit(template="aliyun-new-user", who=NEW, username="xinren")
        env.store.update(
            ticket["id"],
            actor="test",
            expect=[t.PENDING],
            to=t.APPROVED,
            event="approved",
        )
        env.store.update(
            ticket["id"],
            actor="test",
            expect=[t.APPROVED],
            to=t.EXECUTING,
            event="execute_start",
        )
        env.store.update(
            ticket["id"],
            actor="test",
            expect=[t.EXECUTING],
            to=t.DONE,
            event="execute_done",
            fields={"user_created": True},
        )
        note = {o["id"]: o for o in env.flows.options("on_new")}["aliyun-new-user"]["state_note"]
        self.assertIn("xinren", note)


class ApplicantEventTests(unittest.TestCase):
    """申请人在自己单子上看到的那两条事件。

    不填 `_EVENT_LABELS` 的话他看到的是原始事件名 `manual_notice_failed`；
    不填 `_ERROR_NOTES` 的话他看到的是 `notify.notify_admins` 拼的那串
    ——**含管理员 union_id 前 12 位和飞书/urllib 的英文异常**。
    """

    VIEWER = requests_api.Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "u_li", False)

    def test_both_events_have_chinese_labels(self):
        for event in ("manual_notice_sent", "manual_notice_failed"):
            with self.subTest(event=event):
                said = requests_api._EVENT_LABELS.get(event, "")  # noqa: SLF001
                self.assertTrue(said, f"{event} 没有中文说明，申请人会看到原始事件名")
                self.assertNotEqual(said, event)

    def test_the_applicant_never_sees_the_raw_failure(self):
        env = Env()
        env.announce_problems = ["on_admin1234…：TimeoutError: HTTPSConnectionPool(...)"]
        done = env.run()
        view = requests_api.ticket_view(env.store.get(done["id"]), viewer=self.VIEWER)
        body = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("on_admin1234", body, "管理员的 union_id 不该漏给申请人")
        self.assertNotIn("TimeoutError", body)
        self.assertIn("通知管理员没发出去", body)
        # 光说「没发出去」会让他以为这张单卡住了。要接着说下一步是什么
        self.assertIn(requests_api._ERROR_NOTES["manual_notice_failed"], body)  # noqa: SLF001

    def test_the_admin_still_sees_the_raw_failure(self):
        """对照：原始错误是给管理员排障用的，别一起藏掉。"""
        env = Env()
        env.announce_problems = ["on_admin1234…：TimeoutError: HTTPSConnectionPool(...)"]
        done = env.run()
        admin = requests_api.Caller("on_adm", "管理员", "a@wuji.tech", "ou_adm", "u_adm", True)
        body = json.dumps(
            requests_api.ticket_view(env.store.get(done["id"]), viewer=admin), ensure_ascii=False
        )
        self.assertIn("TimeoutError", body)


class FulfillingPushTests(unittest.TestCase):
    """推给**申请人**的那张「审批已通过、等开通」卡 —— 第六个出口。"""

    def how(self, platform, account=JZ):
        return notify_mod._fulfilling_how(  # noqa: SLF001
            {"kind": "account", "template": {"platform": platform, "account": account}}
        )

    def test_it_names_the_platform_the_panel_cannot_reach(self):
        for platform in platforms_mod.MANUAL_IDS:
            with self.subTest(platform=platform):
                said = self.how(platform)
                self.assertIn(platforms_mod.name_of(platform), said)
                self.assertIn("面板开不了", said)
                # 「这类资源」是资源开通那条路的词。对九章说它不算谎话，
                # 但这条链上另外五个出口都改口了 —— 口径不一致，人不知道信哪个
                self.assertNotIn("这类资源", said)

    def test_real_clouds_keep_the_old_wording(self):
        said = self.how("aliyun", ACC)
        self.assertIn("这类资源", said)
        self.assertNotIn("面板开不了", said)

    # 上面两条测的是那个助手本身。**光测助手不够** —— 助手算对了、卡片没调它，
    # 申请人收到的还是老词，而两条用例照样全绿（实测：把 `message()` 那一处
    # 换回硬编码的老句子，上面两条一条都不红）。下面两条走真正的卡片。

    def card(self, platform, account=JZ):
        _color, title, lines = notify_mod.message(
            "fulfilling",
            {
                "id": "REQ-1",
                "kind": "account",
                "template": {"platform": platform, "account": account, "title": "开号"},
                "payload": {},
            },
        )
        return f"{title}\n" + "\n".join(lines)

    def test_the_card_itself_says_it_for_manual_platforms(self):
        said = self.card("jiuzhang")
        self.assertIn("九章", said)
        self.assertIn("面板开不了", said)
        self.assertNotIn("这类资源", said)

    def test_the_card_for_a_real_cloud_is_unchanged(self):
        said = self.card("aliyun", ACC)
        self.assertIn("这类资源", said)
        self.assertNotIn("面板开不了", said)


class TodoPlatformNameTests(unittest.TestCase):
    """待办那条标题里的平台名。直接喂 `collect_tickets`，不绕服务端那一层。"""

    def rows(self, *platforms_in):
        return [
            {
                "manual_account": True,
                "platform": p,
                "kind": "account",
                "state": t.FULFILLING,
                "created_at": "2026-09-20T10:00:00+08:00",
            }
            for p in platforms_in
        ]

    def title(self, *platforms_in):
        report = todo_mod.Report()
        todo_mod.collect_tickets(report, list(self.rows(*platforms_in)))
        hit = [i for i in report.items if i.kind == "request_manual_account"]
        self.assertEqual(len(hit), 1, [i.kind for i in report.items])
        return hit[0].title

    def test_one_platform(self):
        self.assertIn("九章", self.title("jiuzhang"))

    def test_two_platforms_are_both_named(self):
        said = self.title("jiuzhang", "turboai")
        self.assertIn("九章", said)
        self.assertIn("TurboAI", said)

    def test_the_same_platform_twice_is_named_once(self):
        self.assertEqual(self.title("jiuzhang", "jiuzhang").count("九章"), 1)

    def test_an_unknown_platform_leaves_no_dangling_separator(self):
        """认不出的平台名要**跳过**，不是留一个空段。

        留空段的结果是标题里出现「九章、、TurboAI（曦望）」—— 中间那个多余的分隔符
        `.strip()` 擦不掉（它在中间）。同文件的 `_manual_names()` 已经处理了这件事，
        两处该是同一份实现。
        """
        said = self.title("jiuzhang", "newcloud", "turboai")
        self.assertNotIn("、、", said)
        self.assertNotIn("和和", said)


class HealthDefinitionScopeTests(unittest.TestCase):
    """体检查的是**模板指向的那条**审批定义，不是顶层那一份。

    两者可以任意背离（`load_map` 允许某条 definition 自带 `account_options`）：
    配在 definition 里会被顶层查成「缺」（误报，逼人去改一份本来就对的配置）；
    顶层有、definition 覆盖成别的会被查成「有」（漏报，第一个申请人照样提交失败）。
    误报和漏报都会让这条检查失去意义。
    """

    KEY = ("登录与审批", "飞书审批")
    #: definitions 里的每条都要自带这些，否则 `load_map` 不收
    BASE = {
        "approval_code": "APPROVAL-2",
        "widgets": {k: f"w2-{k}" for k in ("ticket_id", "kind", "summary", "reason")},
    }

    def check(self, *, top=None, definitions=None, approval=""):
        data = json.loads(json.dumps(TEMPLATES))
        for spec in data["templates"]:
            if spec["platform"] in platforms_mod.MANUAL_IDS and approval:
                spec["approval"] = approval
        backend = health_test.FakeBackend(_catalog=catalog_mod.parse(data))
        conf = json.loads(Path(backend.approval_path).read_text(encoding="utf-8"))
        if top is not None:
            conf["account_options"] = top
        if definitions is not None:
            conf["definitions"] = definitions
        Path(backend.approval_path).write_text(json.dumps(conf), encoding="utf-8")
        return health_test.run(backend, health_test.with_base())[1][self.KEY]

    def test_options_configured_only_on_the_named_definition_are_enough(self):
        """误报侧：模板指向 `manual`，对照表配在那条 definition 里 —— 不该报 CRIT。"""
        got = self.check(
            approval="manual",
            definitions={
                "manual": {
                    **self.BASE,
                    "account_options": {f"jiuzhang/{JZ}": JZ_OPTION, f"turboai/{TA}": TA_OPTION},
                }
            },
        )
        self.assertEqual(got["level"], health.OK)

    def test_a_definition_that_overrides_the_table_is_caught(self):
        """漏报侧：顶层配齐了，模板却指向一条自带**别的**对照表的 definition。

        送出去的是那条 definition 的表 —— 顶层配得再全，申请照样被飞书拒。
        """
        got = self.check(
            top={f"jiuzhang/{JZ}": JZ_OPTION, f"turboai/{TA}": TA_OPTION},
            approval="manual",
            definitions={"manual": {**self.BASE, "account_options": {"aliyun/1": "别的"}}},
        )
        self.assertEqual(got["level"], health.CRIT)
        self.assertIn(f"jiuzhang/{JZ}", got["detail"])

    def test_the_default_definition_still_reads_the_top_level(self):
        """对照：模板没有 `approval` 字段时读的就是顶层那一份（今天线上就是这样）。"""
        self.assertEqual(
            self.check(top={f"jiuzhang/{JZ}": JZ_OPTION, f"turboai/{TA}": TA_OPTION})["level"],
            health.OK,
        )
        self.assertEqual(self.check(top={})["level"], health.CRIT)


if __name__ == "__main__":
    unittest.main()

"""资源模板的第三种轴：文本。

文本轴的值**整个来自申请人**（别的轴申请人只能「选哪个」，值是模板定死的）。所以这一页
盯两件事：

① **它绝不进云参数** —— 值和参数名两方面都不行。让它影响创建参数就等于把云 API 开放给
   全员，正是 `Template._params` 那句「一个字节都不来自申请人」要挡的事；
② 进审批摘要之前被压成单行、空的拒、超长拒 —— 它会原样出现在审批人眼前那张单子上。

数据全部虚构。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import tickets as t
from delivery.approval import FeishuApproval
from delivery.flows import FlowError, Flows

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import ACC, CONFIG, LI, FakeExecutor, FakeFeishu, _roster

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

NOW = 1_800_000_000.0  # 2027-01-15
UNTIL = "2027-03-01"

#: 一个三种轴都有的资源模板：选项 + 数字 + 文本
BASE = {
    "id": "ecs-axes",
    "kind": "resource",
    "platform": "aliyun",
    "account": ACC,
    "title": "ECS 开发机",
    "resource_type": "ecs",
    "region": "cn-hangzhou",
    "max_days": 365,
    "params": {"VSwitchId": "vsw-1", "SystemDisk.Category": "cloud_essd"},
    "options": [
        {
            "id": "spec",
            "label": "规格",
            "choices": [
                {"id": "s", "label": "小", "params": {"InstanceType": "ecs.g8i.large"}},
                {"id": "l", "label": "大", "params": {"InstanceType": "ecs.g8i.2xlarge"}},
            ],
        },
        {
            "id": "disk",
            "label": "数据盘",
            "number": {
                "min": 40,
                "max": 2000,
                "step": 20,
                "default": 100,
                "unit": "G",
                "param": "DataDisk.Size",
            },
        },
        {"id": "project", "label": "项目", "text": {"max": 60, "hint": "写项目或系统名"}},
    ],
    "cost_centers": [{"id": "algo", "label": "算法组"}],
}


def tpl(**over):
    return catalog_mod.parse_template({**BASE, **over}, 0)


def axes(*items):
    return {**BASE, "options": list(items)}


class TextAxisShapeTests(unittest.TestCase):
    """模板加载期：三选一，写错当场拒。"""

    def test_a_text_axis_loads(self):
        axis = tpl().axis("project")
        self.assertEqual(axis.text, (60, "写项目或系统名"))
        self.assertEqual(axis.choices, ())
        self.assertIsNone(axis.number)

    def test_hint_is_optional(self):
        got = catalog_mod.parse_template(
            axes({"id": "project", "label": "项目", "text": {"max": 30}}), 0
        )
        self.assertEqual(got.axis("project").text, (30, ""))

    def test_exactly_one_shape_per_axis(self):
        """三种轴的语义完全不同（查表 / 钳位 / 原样带走），同时给两种就是没想清楚要哪种。"""
        bad = (
            {"choices": [{"id": "s", "label": "小", "params": {"K": "1"}}], "text": {"max": 10}},
            {"number": {"min": 1, "max": 9, "param": "N"}, "text": {"max": 10}},
            {
                "choices": [{"id": "s", "label": "小", "params": {"K": "1"}}],
                "number": {"min": 1, "max": 9, "param": "N"},
                "text": {"max": 10},
            },
            {},  # 一种都不给
        )
        for shape in bad:
            with self.subTest(shape=sorted(shape)), self.assertRaises(catalog_mod.CatalogError):
                catalog_mod.parse_template(axes({"id": "x", "label": "X", **shape}), 0)

    def test_max_must_be_a_sane_integer(self):
        for value in (0, -1, 201, True, "60", 60.0, None):
            with self.subTest(value=value), self.assertRaises(catalog_mod.CatalogError):
                catalog_mod.parse_template(
                    axes({"id": "x", "label": "X", "text": {"max": value}}), 0
                )

    def test_text_spec_shape_is_checked(self):
        for spec in ("60", 60, [], {"hint": "只有提示"}, {"max": 60, "nope": 1}, {}):
            with self.subTest(spec=spec), self.assertRaises(catalog_mod.CatalogError):
                catalog_mod.parse_template(axes({"id": "x", "label": "X", "text": spec}), 0)

    def test_overlong_hint_is_refused(self):
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(
                axes({"id": "x", "label": "X", "text": {"max": 60, "hint": "长" * 61}}), 0
            )


class TextAxisNeverReachesTheCloudTests(unittest.TestCase):
    """安全要害：文本轴一个字节都不能进创建参数。"""

    def test_the_axis_contributes_no_parameter_at_all(self):
        with_text = tpl().resolved_params({"spec": "s"}, {"disk": 100})
        without_text = catalog_mod.parse_template(
            axes(BASE["options"][0], BASE["options"][1]), 0
        ).resolved_params({"spec": "s"}, {"disk": 100})
        self.assertEqual(with_text, without_text)
        self.assertNotIn("project", with_text)

    def test_a_text_value_smuggled_through_the_other_two_channels_is_ignored(self):
        """前端怎么传都没用：文本轴在 choices / numbers 里出现也一样不出参数。

        `resolved_params` 压根没有「文本」这个入口 —— 所以要挡的是「拿别的入口冒充」。
        """
        evil = "InstanceType=ecs.gn7i-c8g1.2xlarge"
        got = tpl().resolved_params({"spec": "s", "project": evil}, {"disk": 100, "project": evil})
        dumped = json.dumps(got, ensure_ascii=False)
        self.assertNotIn(evil, dumped)
        self.assertNotIn("gn7i", dumped)
        self.assertEqual(got["InstanceType"], "ecs.g8i.large")

    def test_neither_the_value_nor_a_parameter_name_can_come_from_the_applicant(self):
        """参数**名**同样不能来自申请人：多一个键就是多一个能改的云 API 参数。"""
        got = tpl().resolved_params({"spec": "s", "project": "x"}, {"disk": 100})
        self.assertEqual(
            set(got),
            {"VSwitchId", "SystemDisk.Category", "InstanceType", "DataDisk.Size"},
        )

    def test_a_hidden_text_axis_changes_nothing_either(self):
        got = catalog_mod.parse_template(
            axes(
                BASE["options"][0],
                {
                    "id": "project",
                    "label": "项目",
                    "hidden_when": {"spec": ["s"]},
                    "text": {"max": 60},
                },
            ),
            0,
        )
        self.assertEqual(got.hidden_axes({"spec": "s"}), {"project"})
        self.assertEqual(got.resolved_params({"spec": "s"}), got.resolved_params({"spec": "s"}))
        self.assertNotIn("project", got.resolved_params({"spec": "l"}))


class Env:
    def __init__(self, options=None):
        d = Path(tempfile.mkdtemp())
        self.templates = {
            "schema": catalog_mod.SCHEMA,
            "templates": [BASE if options is None else axes(*options)],
        }
        self.feishu = FakeFeishu()
        self.executor = FakeExecutor()
        self.store = t.TicketStore(str(d / "tickets.json"), clock=lambda: NOW)
        approval = FeishuApproval(CONFIG, lambda: "tok", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            clock=lambda: NOW,
        )

    def submit(self, **over):
        payload = {
            "choices": {"spec": "s"},
            "numbers": {"disk": 100},
            "texts": {"project": "舞肌训练平台"},
            "cost_center": "algo",
            "until": UNTIL,
        }
        payload.update(over)
        return self.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id="ecs-axes",
            payload=payload,
            reason="项目需要一台机器跑训练",
        )


class TextAxisSubmitTests(unittest.TestCase):
    """提交时：压成单行、空的拒、超长拒，值只进台账和摘要。"""

    def test_the_value_is_recorded_and_shown_in_the_summary(self):
        ticket = Env().submit()
        self.assertEqual(ticket["payload"]["texts"], {"project": "舞肌训练平台"})
        self.assertIn("项目 舞肌训练平台", ticket["summary"])
        self.assertIn("规格 小", ticket["summary"])

    def test_newlines_are_flattened(self):
        """留着换行就能在审批摘要里伪造出一整行「审批意见：同意」。"""
        ticket = Env().submit(texts={"project": " 舞肌\n\t 训练\r\n平台 "})
        self.assertEqual(ticket["payload"]["texts"]["project"], "舞肌 训练 平台")
        self.assertNotIn("\n", ticket["summary"])

    def test_empty_is_refused(self):
        for value in ("", "   ", "\n", None, 0):
            with self.subTest(value=repr(value)), self.assertRaises(FlowError):
                Env().submit(texts={"project": value})
        with self.assertRaises(FlowError):  # 整个 texts 都没给
            Env().submit(texts={})
        with self.assertRaises(FlowError):
            Env().submit(texts="不是字典")

    def test_length_is_checked_after_flattening_and_counts_characters(self):
        env = Env()
        ok = env.submit(texts={"project": "长" * 60})["payload"]["texts"]["project"]
        self.assertEqual(len(ok), 60)
        with self.assertRaises(FlowError):
            Env().submit(texts={"project": "长" * 61})
        # 先压平再量：61 个字符里夹换行，压平后仍是 61，不能因为「看起来像两行」就放过
        with self.assertRaises(FlowError):
            Env().submit(texts={"project": "长" * 30 + "\n" + "长" * 30})

    def test_unknown_keys_in_texts_are_ignored(self):
        ticket = Env().submit(texts={"project": "舞肌", "nope": "x", "spec": "8 卡 A100"})
        self.assertEqual(ticket["payload"]["texts"], {"project": "舞肌"})
        self.assertNotIn("A100", json.dumps(ticket, ensure_ascii=False))

    def test_a_hidden_text_axis_is_not_asked_for(self):
        """选了小规格就不问项目名 —— 不问就不校验、也不进台账。"""
        env = Env(
            options=(
                BASE["options"][0],
                {
                    "id": "project",
                    "label": "项目",
                    "hidden_when": {"spec": ["s"]},
                    "text": {"max": 60},
                },
            )
        )
        ticket = env.submit(numbers={}, texts={})
        self.assertEqual(ticket["payload"]["texts"], {})
        self.assertNotIn("项目", ticket["summary"])

    def test_what_the_ticket_would_create_never_contains_the_text(self):
        """端到端：拿台账里那份 payload 去算创建参数，文本轴的值不在里面。"""
        env = Env()
        ticket = env.submit(texts={"project": "InstanceType=evil"})
        template = catalog_mod.parse(env.templates).get("ecs-axes")
        params = template.resolved_params(
            ticket["payload"]["choices"], ticket["payload"]["numbers"]
        )
        self.assertNotIn("evil", json.dumps(params, ensure_ascii=False))
        self.assertEqual(params["InstanceType"], "ecs.g8i.large")


class TextAxisFrontendTests(unittest.TestCase):
    """申请页要能画出这个输入框。

    `public()` 是前端拿到的全部信息。不带 text 的话，一个文本轴在页面上和「choices 为空的
    下拉」长得一模一样 —— 申请人选不出任何东西，提交必然被服务端以「没填」拒掉，
    也就是说配了文本轴的模板在网页上根本没法申请。
    """

    def test_public_exposes_the_text_axis(self):
        axis = {a["id"]: a for a in tpl().public()["options"]}["project"]
        self.assertEqual(axis["text"], {"max": 60, "hint": "写项目或系统名"})

    def test_public_still_hides_the_cloud_parameters(self):
        dumped = json.dumps(tpl().public(), ensure_ascii=False)
        self.assertNotIn("vsw-1", dumped)
        self.assertIn("项目", dumped)


if __name__ == "__main__":
    unittest.main()


class ManuallyFulfilledOptionsTests(unittest.TestCase):
    """人工开通的资源也该能问清楚问题。

    之前这条路是堵死的：配 `options` 就必须有 `resource_type`，而 `resource_type`
    只支持 ecs —— 于是 RDS 这种面板建不了的资源只能退化成一个自由文本框，
    审批人看到一句话，开通的人还得回头问。

    而 `flows` 里**所有**资源单都停在「待开通」（「资源开通面板一行云都不写」），
    `resource_type` 根本没被用来建任何东西。那条规则拦的是一件不会发生的事。
    """

    @staticmethod
    def spec(**over):
        base = {
            "id": "rds",
            "kind": "resource",
            "platform": "aliyun",
            "account": "170406579653",
            "title": "RDS",
            "options": [
                {
                    "id": "engine",
                    "label": "引擎",
                    "choices": [{"id": "mysql", "label": "MySQL 8.0", "params": {}}],
                },
                {
                    "id": "storage",
                    "label": "存储",
                    "number": {"min": 20, "max": 3000, "default": 100, "unit": " GB"},
                },
            ],
        }
        base.update(over)
        return base

    def test_options_without_a_resource_type_are_allowed(self):
        tpl = catalog_mod.parse_template(self.spec(), 0)
        self.assertEqual(tpl.resource_type, "")
        self.assertEqual([a.id for a in tpl.options], ["engine", "storage"])

    def test_a_number_axis_needs_no_cloud_param_when_nobody_will_build_it(self):
        tpl = catalog_mod.parse_template(self.spec(), 0)
        self.assertEqual(tpl.options[1].number.param, "")
        # 而且它不会往云参数里塞一个空键
        self.assertEqual(tpl.resolved_params({}, {"storage": 200}), {})

    def test_cloud_params_without_a_resource_type_are_refused_as_a_mistake(self):
        """没有 resource_type 却填了云参数 —— 那不是「人工开通」，是配错了，
        而错的表现是「填了但永远没人读」。"""
        bad = self.spec(
            options=[
                {
                    "id": "storage",
                    "label": "存储",
                    "number": {"param": "DBInstanceStorage", "min": 20, "max": 100, "default": 20},
                },
            ]
        )
        with self.assertRaises(catalog_mod.CatalogError) as caught:
            catalog_mod.parse_template(bad, 0)
        self.assertIn("没人会读", str(caught.exception))

    def test_choice_params_without_a_resource_type_are_refused_too(self):
        bad = self.spec(
            options=[
                {
                    "id": "engine",
                    "label": "引擎",
                    "choices": [{"id": "mysql", "label": "MySQL", "params": {"Engine": "MySQL"}}],
                },
            ]
        )
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(bad, 0)

    def test_base_params_still_require_a_resource_type(self):
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(self.spec(params={"ZoneId": "cn-hangzhou-b"}), 0)


class StorageAndTransferValidationTests(unittest.TestCase):
    """数据目录 / 数据迁移的入参校验。

    这两类的 payload 里全是**会进 OSS key 和 RAM 策略 `oss:Prefix` 条件**的字符串，
    所以前端拦过一遍之后这里还要再拦一遍：前端是给人用的，不是安全边界。
    """

    @staticmethod
    def flows_of(tpl_spec):

        from delivery.flows import Flows

        tpl = catalog_mod.parse_template(tpl_spec, 0)
        f = Flows.__new__(Flows)  # 只测纯校验，不需要 store / approval
        return f, tpl

    DIR_TPL = {
        "id": "oss-dir",
        "kind": "storage",
        "platform": "aliyun",
        "account": "170406579653",
        "title": "目录",
        "buckets": [{"name": "wuji-data", "region": "cn-hangzhou"}],
        "stages": {"wuji-data": ["raw", "opensource"]},
    }
    MOVE_TPL = {
        "id": "oss-move",
        "kind": "transfer",
        "platform": "aliyun",
        "account": "170406579653",
        "title": "迁移",
        "buckets": [
            {"name": "wuji-a", "region": "cn-hangzhou"},
            {"name": "wuji-b", "region": "cn-hangzhou"},
            {"name": "tos-c", "region": "cn-shanghai"},
        ],
        "filesystems": [
            {"id": "bmcpfs-1", "region": "cn-hangzhou"},
            {"id": "vepfs-1", "region": "cn-shanghai"},
        ],
    }

    def dir_ok(self, **over):
        f, tpl = self.flows_of(self.DIR_TPL)
        payload = {"bucket": "wuji-data", "stage": "raw", "batch": "20260920-ego-kitchen"}
        payload.update(over)
        return f._validate_storage(tpl, payload, "x")

    def test_a_good_directory_request_yields_the_full_path(self):
        clean, summary = self.dir_ok()
        self.assertEqual(clean["path"], "wuji-data/raw/20260920-ego-kitchen/")
        self.assertIn("cn-hangzhou", summary)

    def test_a_bucket_outside_the_template_is_refused(self):
        with self.assertRaises(FlowError):
            self.dir_ok(bucket="someone-elses-bucket")

    def test_a_stage_the_bucket_does_not_hold_is_refused(self):
        """哪个桶放哪类数据是策略，写在模板里。"""
        with self.assertRaises(FlowError):
            self.dir_ok(stage="delivery")

    def test_a_batch_id_that_could_escape_a_prefix_condition_is_refused(self):
        """`*` 或 `../` 能让一条 RAM 策略覆盖到别人的数据。"""
        for bad in ("../etc", "a/b", "a*b", "a b", "", "-lead", "x" * 70):
            with self.assertRaises(FlowError, msg=bad):
                self.dir_ok(batch=bad)

    def test_open_source_data_must_declare_where_it_came_from(self):
        """出合规问题时这是唯一能自证的东西。"""
        with self.assertRaises(FlowError):
            self.dir_ok(stage="opensource", license="")
        clean, _ = self.dir_ok(stage="opensource", license="CC-BY-4.0")
        self.assertEqual(clean["license"], "CC-BY-4.0")

    def move_ok(self, **over):
        f, tpl = self.flows_of(self.MOVE_TPL)
        payload = {"source": "oss://wuji-a/x/", "dest": "oss://wuji-b/y/"}
        payload.update(over)
        return f._validate_transfer(tpl, payload, "x")

    def test_a_good_move_normalises_both_sides(self):
        clean, summary = self.move_ok()
        self.assertEqual(clean["source"], "oss://wuji-a/x/")
        self.assertIn("跳过同名", summary)

    def test_a_bucket_outside_the_template_is_refused_on_both_sides(self):
        for side in ("source", "dest"):
            with self.assertRaises(FlowError, msg=side):
                self.move_ok(**{side: "oss://not-ours/x/"})

    def test_preheat_and_sink_pass_submit_when_the_filesystem_is_registered(self):
        clean, _ = self.move_ok(dest="cpfs://bmcpfs-1/d/")
        self.assertEqual(clean["dest"], "cpfs://bmcpfs-1/d/")

    def test_an_unregistered_filesystem_is_refused_at_submit(self):
        with self.assertRaises(FlowError):
            self.move_ok(dest="cpfs://bmcpfs-other/d/")

    def test_a_filesystem_under_the_wrong_scheme_is_refused_at_submit(self):
        """`cpfs://vepfs-…` 会拿阿里的凭证去调火山的文件系统 —— 审批通过后执行时才炸。"""
        with self.assertRaises(FlowError):
            self.move_ok(dest="cpfs://vepfs-1/d/")

    def test_a_chain_that_cannot_exist_is_refused_before_approval(self):
        """cpfs→tos、vepfs→cpfs 原先能走完整个飞书审批，到执行时才被拒 ——
        白等一轮审批，还留一张要人工处理的失败单（审计 L-1）。"""
        for src, dst in (
            ("cpfs://bmcpfs-1/a/", "tos://tos-c/"),
            ("vepfs://vepfs-1/a/", "cpfs://bmcpfs-1/b/"),
        ):
            with self.assertRaises(FlowError, msg=f"{src}->{dst}"):
                self.move_ok(source=src, dest=dst)

    def test_paths_that_could_escape_are_refused(self):
        for bad in (
            "oss://wuji-a/../x/",
            "oss://wuji-a/a//b/",
            "oss://wuji-a/a*/",
            "oss://wuji-a/nofinalslash",
            "notascheme://wuji-a/x/",
        ):
            with self.assertRaises(FlowError, msg=bad):
                self.move_ok(source=bad)

    def test_moving_something_onto_itself_is_refused(self):
        with self.assertRaises(FlowError):
            self.move_ok(dest="oss://wuji-a/x/")

    def test_overwrite_is_never_the_silent_default(self):
        """覆盖不可逆，不该是默认值。"""
        clean, _ = self.move_ok()
        self.assertEqual(clean["overwrite"], "skip")
        with self.assertRaises(FlowError):
            self.move_ok(overwrite="yes-please")

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

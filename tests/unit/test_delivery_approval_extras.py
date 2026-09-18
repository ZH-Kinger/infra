"""按类型的审批表单字段（extra / _approval_fields）、控件对照表。

这一页刻意**走到发出去的请求体**：`FeishuApproval` 的假 transport 收的就是飞书会收到的
那个 form JSON。只在函数出入口比对 dict 的话，「哪个值最后被贴到审批单上」这件事就没人看，
而审批单上的那几行正是审批人唯一会读的东西。数据全部虚构。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import flows as flows_mod
from delivery import tickets as t
from delivery.approval import (
    ApprovalConfig,
    ApprovalError,
    FeishuApproval,
)
from delivery.flows import Flows

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import (
    ACC,
    BUCKET,
    CONFIG,
    LI,
    REGION,
    FakeExecutor,
    FakeFeishu,
    _roster,
)

#: 访问凭证要取件地址，没配 flows 在提交那一刻就拒。模块级设、跑完还原
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

NOW = 1_800_000_000.0  # 2027-01-15
#: 四个必填控件 + 按类型补充的那些。键是审批定义里的 custom_id，值是飞书自己生成的 widget id
WIDGETS = {
    **dict(CONFIG.widgets),
    "account": "w-account",
    "subject": "w-subject",
    "scope": "w-scope",
    "caps": "w-caps",
    "valid": "w-valid",
    "spec": "w-spec",
    "cost": "w-cost",
    "until": "w-until",
    "project": "w-project",
    "env": "w-env",
    "cloud_user": "w-cloud-user",
}
FULL = ApprovalConfig(
    approval_code="APPROVAL-2", widgets=WIDGETS, comment_open_id=CONFIG.comment_open_id
)


#: 「这个参数没给」——不能用 None，None 本身就是要测的取值之一
_KEEP = object()


def form_of(feishu, index=-1):
    """某次发起审批时真正发出去的表单：{widget id: 值}。"""
    body = feishu.calls[index][2]
    return {x["id"]: x["value"] for x in json.loads(body["form"])}


def form_list(feishu, index=-1):
    return json.loads(feishu.calls[index][2]["form"])


class ExtraFormFieldsTests(unittest.TestCase):
    def setUp(self):
        self.feishu = FakeFeishu()
        self.approval = FeishuApproval(FULL, lambda: "tok", transport=self.feishu)

    def create(self, extra):
        return self.approval.create(
            ticket_id="REQ-1",
            kind="credential",
            summary="摘要",
            reason="理由",
            applicant=LI,
            extra=extra,
        )

    def test_extra_values_land_on_the_widget_named_by_their_custom_id(self):
        self.create({"subject": "某某公司", "valid": "365 天"})
        form = form_of(self.feishu)
        self.assertEqual(form["w-subject"], "某某公司")
        self.assertEqual(form["w-valid"], "365 天")

    def test_unknown_custom_ids_are_skipped_instead_of_killing_the_request(self):
        """新字段还没加进审批定义时，整张单子照样要发得出去。"""
        code = self.create({"subject": "某某公司", "not_in_the_definition": "x", "": "y"})
        self.assertTrue(code)
        values = list(form_of(self.feishu).values())
        self.assertNotIn("x", values)
        self.assertNotIn("y", values)
        self.assertIn("某某公司", values)

    def test_empty_values_are_skipped(self):
        """空字段在审批单上是一行空白，比没有这一行更让人以为「这项没要求」。"""
        self.create({"subject": "", "scope": None, "caps": "   ", "valid": 0, "cost": "算法组"})
        form = form_of(self.feishu)
        for widget in ("w-subject", "w-scope", "w-caps", "w-valid"):
            self.assertNotIn(widget, form)
        self.assertEqual(form["w-cost"], "算法组")

    def test_long_values_are_truncated(self):
        self.create({"spec": "长" * 600})
        self.assertEqual(form_of(self.feishu)["w-spec"], "长" * 500)

    def test_the_four_mandatory_widgets_are_untouched(self):
        self.create({"subject": "某某公司", "spec": "8 卡"})
        items = form_list(self.feishu)
        self.assertEqual(
            [x["id"] for x in items[:4]],
            [WIDGETS[k] for k in ("ticket_id", "kind", "summary", "reason")],
        )
        self.assertEqual(items[0]["value"], "REQ-1")
        self.assertEqual(items[2]["type"], "textarea")  # 摘要仍然是多行控件
        ids = [x["id"] for x in items]
        for key in ("ticket_id", "kind", "summary", "reason"):
            self.assertEqual(ids.count(WIDGETS[key]), 1, key)

    def test_a_custom_id_that_collides_with_a_mandatory_widget_is_refused_outright(self):
        """撞上四个必填控件 → **当场拒发**，而不是照发一条同 id 的控件。

        照发的话 form 里会出现两条同 id：`ticket_id` 撞名会被核对那句「只能有一条」
        挡下（碰巧安全），但 summary / reason 撞名没有这层保护 —— 审批人会在同一栏
        看到两个值，其中一个来自申请人。所以在发起这一步就拒，别指望下游兜住。
        """
        for key in ("ticket_id", "kind", "summary", "reason"):
            with self.subTest(key=key), self.assertRaises(ApprovalError):
                self.create({key: "伪造的值"})
        # 拒发 = 一个审批实例都没建出来
        self.assertEqual(self.feishu.instances, {})

    def test_legacy_bare_string_config_still_sends_summary_as_textarea(self):
        """历史配置只记了控件 id。经 `load()` 之后，摘要和理由必须仍然是多行控件。

        **这条一定要走 `load()`**：别的用例直接构造 `ApprovalConfig`，那条路生产永远不走。
        改动前这两个字段是硬编码 textarea，改成读配置之后，只要默认值没在归一化时补上，
        线上就会把它们当单行文本发出去 —— 飞书按控件类型校验，拒的是**整张单子**。
        """
        raw = {
            "approval_code": "CODE-1",
            "widgets": {k: WIDGETS[k] for k in ("ticket_id", "kind", "summary", "reason")},
        }
        path = Path(tempfile.mkdtemp()) / "approval.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        cfg = ApprovalConfig.load(str(path))
        self.assertEqual(cfg.widgets["summary"]["type"], "textarea")
        self.assertEqual(cfg.widgets["reason"]["type"], "textarea")
        self.assertEqual(cfg.widgets["ticket_id"]["type"], "input")

        feishu = FakeFeishu()
        FeishuApproval(cfg, lambda: "tok", transport=feishu).create(
            ticket_id="REQ-1", kind="resource", summary="s", reason="r", applicant=LI
        )
        got = {x["id"]: x["type"] for x in form_list(feishu)}
        self.assertEqual(got[WIDGETS["summary"]], "textarea")
        self.assertEqual(got[WIDGETS["reason"]], "textarea")

    def send(self, widget, value):
        """用一个指定类型的控件发一张单，返回飞书收到的那一条控件数据。"""
        cfg = ApprovalConfig(
            approval_code="APPROVAL-2",
            widgets={**WIDGETS, "env": widget},
            comment_open_id=CONFIG.comment_open_id,
        )
        feishu = FakeFeishu()
        FeishuApproval(cfg, lambda: "tok", transport=feishu).create(
            ticket_id="REQ-1",
            kind="resource",
            summary="s",
            reason="r",
            applicant=LI,
            extra={"env": value},
        )
        return next(x for x in form_list(feishu) if x["id"] == "w-env")

    def test_single_choice_widget_is_sent_as_the_option_key(self):
        """单选：形状同文本，但**不能被那条 fail-closed 拦掉**。

        这是整套类型分派唯一的实战形态 —— 线上审批定义里申请类型/云账号/权限/使用环境/
        成本归属五栏都是单选。漏了这条分支，配置里一出现 radioV2 就拒发**整张单子**。
        `radio` 是飞书的旧名，后台人工建的控件可能回它。
        """
        for wtype in ("radioV2", "radio"):
            with self.subTest(type=wtype):
                got = self.send({"id": "w-env", "type": wtype}, "prod")
                self.assertEqual((got["type"], got["value"]), (wtype, "prod"))

    def test_date_widget_is_sent_as_rfc3339_with_timezone(self):
        """日期：飞书只收带时区的 RFC3339，裸日期串会被拒掉整张单子。"""
        got = self.send({"id": "w-env", "type": "date"}, "2026-12-16")
        self.assertEqual(got["type"], "date")
        self.assertEqual(got["value"], "2026-12-16T00:00:00+08:00")
        # 已经带时间的原样放行，不重复拼
        self.assertEqual(
            self.send({"id": "w-env", "type": "date"}, "2026-12-16T09:30:00+08:00")["value"],
            "2026-12-16T09:30:00+08:00",
        )

    def test_multi_choice_widget_is_sent_as_an_array(self):
        """多选：value 是数组不是逗号串。面板内部用「、」拼，发出去要拆回来。"""
        for wtype in ("checkboxV2", "checkbox"):
            with self.subTest(type=wtype):
                got = self.send({"id": "w-env", "type": wtype}, "查看清单、下载")
                self.assertEqual(got["type"], wtype)
                self.assertEqual(got["value"], ["查看清单", "下载"])

    def test_an_unknown_widget_type_is_refused_before_sending(self):
        """没接过的类型**当场拒发**，不要蒙成文本送出去。

        dateInterval / amount / fieldList 的 value 都不是字符串，按文本发一定失败，
        而那时飞书的报错指向控件、查不到这里。这条同时钉住那句 raise ——
        别让人以后为了「修 radioV2」把它直接删掉。
        """
        for wtype in ("dateInterval", "amount", "fieldList"):
            with self.subTest(type=wtype), self.assertRaises(ApprovalError):
                self.send({"id": "w-env", "type": wtype}, "x")

    def test_no_extra_at_all_keeps_the_old_four_field_form(self):
        self.create(None)
        self.assertEqual(len(form_list(self.feishu)), 4)


class WidgetLookupTests(unittest.TestCase):
    """custom_id 才是稳定别名：widget id 是飞书生成的，管理员改一次表单就会漂移。"""

    def approval(self, form):
        calls = []

        def transport(method, url, token, body):
            calls.append((method, url))
            return {"code": 0, "data": {"form": json.dumps(form)}}

        self.calls = calls
        return FeishuApproval(FULL, lambda: "tok", transport=transport)

    FORM = [
        {"id": "widget-1", "type": "input", "name": "申请单号", "custom_id": "ticket_id"},
        {"id": "widget-2", "type": "input", "name": "使用方", "custom_id": "subject"},
        {"id": "widget-3", "type": "textarea", "name": "说明"},  # 没设 custom_id
    ]

    def test_widgets_reports_the_custom_id_including_when_it_is_absent(self):
        got = self.approval(self.FORM).widgets("APPROVAL-2")
        self.assertEqual([x["custom_id"] for x in got], ["ticket_id", "subject", ""])
        self.assertEqual(got[1]["name"], "使用方")

    def test_widget_map_is_custom_id_to_widget_id(self):
        got = self.approval(self.FORM).widget_map("APPROVAL-2")
        self.assertEqual(got, {"ticket_id": "widget-1", "subject": "widget-2"})

    def test_widget_map_of_a_form_without_custom_ids_is_empty_not_wrong(self):
        got = self.approval([{"id": "widget-9", "type": "input", "name": "x"}]).widget_map("A")
        self.assertEqual(got, {})

    def test_broken_form_is_an_approval_error_not_a_crash(self):
        approval = FeishuApproval(
            FULL, lambda: "tok", transport=lambda *a: {"code": 0, "data": {"form": "{不是 JSON"}}
        )
        with self.assertRaises(ApprovalError):
            approval.widget_map("APPROVAL-2")


class ApprovalFieldsTests(unittest.TestCase):
    """`_approval_fields`：审批人要一眼看清「谁、什么权限、多久」，不该逐字读一段话。"""

    CRED = {
        "kind": "credential",
        "platform": "aliyun",
        "account": ACC,
        "caps": ["list", "download"],
    }
    RES = {"kind": "resource", "platform": "aliyun", "account": ACC}

    def fields(self, tpl, payload):
        return flows_mod._approval_fields(tpl, payload)

    def test_credential_fields(self):
        got = self.fields(
            self.CRED,
            {"bucket": BUCKET, "prefix": "team/data/", "hours": 24 * 365, "subject": "某某公司"},
        )
        self.assertEqual(got["subject"], "某某公司")
        self.assertEqual(got["scope"], f"oss://{BUCKET}/team/data/")
        self.assertEqual(got["caps"], "list_download")  # 单选控件的选项 key
        self.assertEqual(got["valid"], "365 天")
        # 云账号送 `平台/账号`：同一朵云下可能有多个主账号，只送平台的话
        # 两个账号的申请在审批单上分不开，而审批人正是按这一栏担责的
        self.assertEqual(got["account"], f"aliyun/{ACC}")

    def test_credential_scope_uses_the_platforms_own_scheme(self):
        """火山的桶写成 oss:// 会让审批人以为申请错了云。"""
        tpl = {**self.CRED, "platform": "volcano", "account": "2000000001"}
        got = self.fields(tpl, {"bucket": "wuji-tos", "prefix": "", "hours": 2})
        self.assertEqual(got["scope"], "tos://wuji-tos/")
        self.assertEqual(got["valid"], "2 小时")

    def test_resource_fields(self):
        got = self.fields(
            self.RES,
            {
                "spec": "规格 小、项目 舞肌、环境 生产",
                "cost_center": "algo",
                "cost_center_name": "算法组",
                "until": "2027-03-01",
                "texts": {"project": "舞肌"},
                "choices": {"env": "prod"},
            },
        )
        self.assertEqual(got["spec"], "规格 小、项目 舞肌、环境 生产")
        self.assertEqual(got["until"], "2027-03-01")
        self.assertEqual(got["project"], "舞肌")
        self.assertEqual(got["env"], "prod")  # 选项 key，不是显示名
        self.assertEqual(got["cost"], "algo")  # 单选控件的选项 key

    def test_other_kinds_report_who_the_account_is_for(self):
        self.assertEqual(
            self.fields(
                {"kind": "account", "platform": "aliyun", "account": ACC}, {"username": "lisi"}
            )["cloud_user"],
            "lisi",
        )
        self.assertEqual(
            self.fields(
                {"kind": "permission", "platform": "aliyun", "account": ACC},
                {"cloud_user": "lisi", "days": 30},
            )["cloud_user"],
            "lisi",
        )

    def test_a_payload_missing_everything_does_not_blow_up(self):
        """缺字段就留空（空值在 create 里会被跳过），绝不能让整张单子发不出去。"""
        for tpl in (self.CRED, self.RES, {"kind": "account", "platform": "aliyun", "account": ACC}):
            for payload in ({}, {"texts": None}, {"hours": 0}):
                with self.subTest(kind=tpl["kind"], payload=payload):
                    got = self.fields(tpl, payload)
                    self.assertIsInstance(got, dict)
                    self.assertTrue(all(isinstance(v, str) for v in got.values()), got)

    def test_a_texts_that_is_not_a_dict_does_not_blow_up(self):
        """`texts` 的类型不能想当然：这张单子最坏也只该少几行，不该整个提交失败。

        目前 submit 传进来的是**申请人原样提交**的 payload，所以 `{"texts": "x"}` 这种
        提交会一路走到这里；就算改成传校验后的那份，这里也该自己挡一道。
        """
        for bad in ("不是字典", ["project"], 3):
            with self.subTest(bad=bad):
                got = self.fields(self.RES, {"texts": bad})
                self.assertEqual(got["project"], "")

    def test_none_of_the_field_names_collide_with_the_four_mandatory_widgets(self):
        """重名会在表单里多出一条同 id 的控件，核对时直接判「单号对不上」。"""
        mandatory = {"ticket_id", "kind", "summary", "reason"}
        for tpl in (
            self.CRED,
            self.RES,
            {"kind": "account", "platform": "aliyun", "account": ACC},
        ):
            with self.subTest(kind=tpl["kind"]):
                self.assertEqual(set(self.fields(tpl, {})) & mandatory, set())


#: 带文本轴的资源模板 + 一个凭证模板。审批表单上的字段就是从这两类单子拆出来的
TEMPLATES = {
    "schema": catalog_mod.SCHEMA,
    "templates": [
        {
            "id": "ecs-axes",
            "kind": "resource",
            "platform": "aliyun",
            "account": ACC,
            "title": "ECS 开发机",
            "resource_type": "ecs",
            "region": "cn-hangzhou",
            "max_days": 365,
            "params": {"VSwitchId": "vsw-1"},
            "options": [
                {
                    "id": "spec",
                    "label": "规格",
                    "choices": [
                        {"id": "s", "label": "小", "params": {"InstanceType": "ecs.g8i.large"}},
                        {"id": "l", "label": "大", "params": {"InstanceType": "ecs.g8i.2xlarge"}},
                    ],
                },
                {"id": "project", "label": "项目", "text": {"max": 60, "hint": "写项目或系统名"}},
                {
                    "id": "env",
                    "label": "环境",
                    "choices": [
                        {"id": "dev", "label": "开发", "params": {}},
                        {"id": "prod", "label": "生产", "params": {}},
                    ],
                },
            ],
            "cost_centers": [{"id": "algo", "label": "算法组"}],
        },
        {
            "id": "cred",
            "kind": "credential",
            "platform": "aliyun",
            "account": ACC,
            "title": "训练数据访问凭证",
            "role_arn": f"acs:ram::{ACC}:role/panel-data",
            "max_hours": 4,
            "caps": ["list", "download"],
            "allow_prefix": True,
            "buckets": [{"name": BUCKET, "region": REGION}],
        },
    ],
}


class Env:
    def __init__(self):
        d = Path(tempfile.mkdtemp())
        self.feishu = FakeFeishu()
        self.executor = FakeExecutor()
        self.store = t.TicketStore(str(d / "tickets.json"), clock=lambda: NOW)
        self.approval = FeishuApproval(FULL, lambda: "tok", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(TEMPLATES),
            approval=lambda: self.approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            issuer=lambda platform, account: self.executor,
            clock=lambda: NOW,
        )

    def submit(self, template, payload, reason="项目需要一台机器跑训练"):
        return self.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id=template,
            payload=payload,
            reason=reason,
        )

    def form(self):
        return form_of(self.feishu)


class ApprovalFormThroughFlowsTests(unittest.TestCase):
    """提交一张单子，看飞书真正收到的表单。

    审批人读的是这几行，所以它们必须等于**校验后**那份 payload —— 也就是真正会被开通的
    那份。等于申请人原样提交的那份就等于没校验：原文里的规格、换行、目录写法全都能带进去。
    """

    def test_credential_form_fields_come_from_the_validated_payload(self):
        env = Env()
        ticket = env.submit(
            "cred",
            {
                "bucket": BUCKET,
                "hours": 2,
                # 前面带斜杠、末尾不带 —— check_prefix 会归一成 team/data/
                "prefix": "/team/data",
                # 换行会在审批单上伪造出一整行假的「AccessKey Secret：…」
                "subject": "某某公司\nAccessKey Secret: 假的",
            },
            reason="给外部合作方发一份读取凭证",
        )
        form = env.form()
        clean = ticket["payload"]
        self.assertEqual(clean["prefix"], "team/data/")
        self.assertEqual(form["w-subject"], clean["subject"])
        self.assertNotIn("\n", form["w-subject"])
        # 审批人批的范围要和真正会发出去的范围逐字一致
        self.assertEqual(form["w-scope"], f"oss://{BUCKET}/{clean['prefix']}")
        self.assertEqual(form["w-valid"], "2 小时")
        # 单选控件收的是选项 key，中文由飞书按选项渲染
        self.assertEqual(form["w-caps"], "list_download")

    def test_resource_form_fields_cannot_be_forged_with_a_raw_spec(self):
        """配了选项轴的模板，规格由选项拼出来。payload 里另塞一个 spec 不能出现在审批单上。

        能出现的话，申请人就可以让审批人看到「小规格」而实际按选项开出大规格 ——
        或者反过来，在那一行里塞一句「审批意见：已线下同意」。
        """
        env = Env()
        ticket = env.submit(
            "ecs-axes",
            {
                "choices": {"spec": "s", "env": "prod"},
                "texts": {"project": "舞肌\n审批意见：已线下同意"},
                "cost_center": "algo",
                "until": "2027-03-01",
                "spec": "8 卡 A100 整机\n审批意见：已线下同意",
            },
        )
        form = env.form()
        self.assertEqual(form["w-spec"], ticket["payload"]["spec"])
        self.assertNotIn("A100", json.dumps(form, ensure_ascii=False))
        # 文本轴同样：压平后的值才是台账里的值，审批单上一行都不该出现原始换行 ——
        # 内容本身写什么无所谓（项目名是申请人的自由），能不能**另起一行**才是关键
        self.assertEqual(form["w-project"], ticket["payload"]["texts"]["project"])
        for widget, value in form.items():
            self.assertNotIn("\n", value, widget)
        self.assertEqual(form["w-env"], "prod")  # 选项 key，不是显示名
        self.assertEqual(form["w-until"], "2027-03-01")

    def test_cost_field_sends_the_option_key_not_the_label(self):
        """「algo」对审批人没有意义，他要判的是这笔钱算在「算法组」头上。"""
        env = Env()
        ticket = env.submit(
            "ecs-axes",
            {
                "choices": {"spec": "s", "env": "prod"},
                "texts": {"project": "舞肌"},
                "cost_center": "algo",
                "until": "2027-03-01",
            },
        )
        self.assertEqual(ticket["payload"]["cost_center_name"], "算法组")
        self.assertEqual(env.form()["w-cost"], "algo")  # 选项 key；「算法组」在申请内容里

    def test_the_form_still_carries_the_four_mandatory_fields(self):
        env = Env()
        ticket = env.submit(
            "ecs-axes",
            {
                "choices": {"spec": "s", "env": "prod"},
                "texts": {"project": "舞肌"},
                "cost_center": "algo",
                "until": "2027-03-01",
            },
        )
        form = env.form()
        self.assertEqual(form[WIDGETS["ticket_id"]], ticket["id"])
        self.assertEqual(form[WIDGETS["kind"]], "resource")  # 条件分支按这个 key 分流
        self.assertEqual(form[WIDGETS["summary"]], ticket["summary"])

    def test_a_definition_without_the_extra_widgets_still_submits(self):
        """审批定义还没加这些控件时，单子要照常发得出去（只是少几行）。"""
        env = Env()
        env.approval = FeishuApproval(CONFIG, lambda: "tok", transport=env.feishu)
        env.flows._approval = lambda: env.approval
        ticket = env.submit(
            "ecs-axes",
            {
                "choices": {"spec": "s", "env": "prod"},
                "texts": {"project": "舞肌"},
                "cost_center": "algo",
                "until": "2027-03-01",
            },
        )
        self.assertEqual(ticket["status"], t.PENDING)
        self.assertEqual(len(form_list(env.feishu)), 4)


if __name__ == "__main__":
    unittest.main()

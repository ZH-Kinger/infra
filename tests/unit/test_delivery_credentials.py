"""访问凭证发放与资源开通：策略文档、长短期分流、发放失败的清理、资源单的「待开通」。

这一块是整个面板里最容易出安全事故的地方，所以断言都往「线上真踩过的坑」上钉：

  · 桶信息语句独立成条、桶级、**绝不叠前缀条件** —— 叠了就是「凭证发了但什么也干不了」；
  · caps 只要非空就给桶信息 —— 只勾「上传」的使用方同样要先探桶；
  · 每条 Allow 都叠时间窗（桶信息那条也要）—— 漏了就等于到期后还能调；
  · 会话策略超长**抛错不截断** —— 截断掉的往往正是那条 Deny；
  · 火山 AssumeRole 收到会话策略必须报错 —— 静默忽略 = 把整个角色的范围发出去；
  · 凭证只走审批评论，secret 不进申请单、不进事件、不进通知卡。

数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import grants, platforms, requests_api
from delivery import notify as notify_mod
from delivery import people as people_mod
from delivery import tickets as t
from delivery.approval import Applicant, FeishuApproval
from delivery.clouds import volcano
from delivery.flows import FlowError, Flows
from delivery.provision import ProvisionError, VolcanoExecutor

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import (
    ACC,
    APPROVAL_JSON,
    BUCKET,
    CONFIG,
    FakeExecutor,
    FakeFeishu,
    view_key,
    view_lines,
)

#: 取件地址（DELIVERY_BASE_URL）是凭证的唯一出口：没配 flows 在提交那一刻就拒。
#: 模块级设、跑完还原 —— 不用全局 autouse，否则「没配就该拒」那条路再也测不到。
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

#: (平台, 该平台的对象存储方言)。方言表在 delivery/platforms.py，两朵云共用一份策略生成逻辑
DIALECTS = (("aliyun", platforms.ALIYUN.storage), ("volcano", platforms.VOLCANO.storage))

VOLC = "2000000001"
TOS_BUCKET = "wuji-tos-data"
SHENZHEN = "wuji-sz-data"
LI = Applicant(union_id="on_li", name="李四", open_id="ou_li")
NEW = Applicant(union_id="on_new", name="新人", open_id="ou_new")

TEMPLATES = {
    "schema": catalog_mod.SCHEMA,
    "templates": [
        {
            # 阿里云凭证：配了角色，≤12 小时走 STS，超过走长期
            "id": "ali-data",
            "kind": "credential",
            "platform": "aliyun",
            "account": ACC,
            "title": "训练数据访问凭证",
            "role_arn": f"acs:ram::{ACC}:role/panel-data",
            "max_hours": catalog_mod.MAX_CREDENTIAL_HOURS,
            "caps": ["list", "download"],
            "buckets": [
                {"name": BUCKET, "region": "cn-hangzhou"},
                {"name": SHENZHEN, "region": "cn-shenzhen"},
            ],
        },
        {
            # 火山凭证：一律长期路径（不配、也不许配角色）
            "id": "volc-data",
            "kind": "credential",
            "platform": "volcano",
            "account": VOLC,
            "title": "火山数据访问凭证",
            "max_hours": 720,
            "caps": ["download", "write"],
            "buckets": [{"name": TOS_BUCKET, "region": "cn-beijing"}],
            "allow_prefix": False,
        },
        {
            # 资源开通：面板一行云都不写，停在「待开通」等管理员按 IaC 建好回来登记。
            # 刻意不配套餐清单：这里要锁的是「人工开通」这条路，不依赖清单字段怎么写
            "id": "ecs-box",
            "kind": "resource",
            "platform": "aliyun",
            "account": ACC,
            "title": "ECS 开发机",
            "max_days": 90,
            "spec_hint": "写清楚要几核几 G",
        },
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


def _roster():
    proposal = {
        "domain": "wuji.tech",
        "people": [
            {
                "email": "li.si@wuji.tech",
                "links": [
                    {"scope": f"aliyun/{ACC}", "name": "lisi", "status": "confirmed"},
                    {"scope": f"volcano/{VOLC}", "name": "lisi-volc", "status": "confirmed"},
                ],
            }
        ],
        "unlinked": [],
        "services": [],
    }
    built = people_mod.build(proposal, [])
    built["people"][0]["union_id"] = "on_li"
    built["people"].append(
        {
            "union_id": "on_new",
            "name": "新人",
            "email": "new@wuji.tech",
            "accounts": [],
            "pending": [],
        }
    )
    return people_mod.parse(built)


class Env:
    """一套 Flows：开通身份和发放身份是两个不同的假执行器（云上那两把 AK 也是分开的）。"""

    def __init__(self, templates=TEMPLATES, **kw):
        self.dir = Path(tempfile.mkdtemp())
        self.templates = json.loads(json.dumps(templates))
        self.feishu = FakeFeishu()
        self.executor = FakeExecutor()
        self.issuer = FakeExecutor()
        self.exec_calls = []
        self.issuer_calls = []
        self.notices = []
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        self.approval = FeishuApproval(CONFIG, lambda: "tok", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: self.approval,
            roster=_roster,
            executor=self._exec,
            issuer=self._issue,
            notify=lambda event, ticket: self.notices.append(
                (event, json.loads(json.dumps(ticket)))
            ),
            clock=lambda: self.now[0],
            **kw,
        )

    def _exec(self, platform, account):
        self.exec_calls.append((platform, account))
        return self.executor

    def _issue(self, platform, account):
        self.issuer_calls.append((platform, account))
        return self.issuer

    def submit(self, template="ali-data", payload=None, applicant=LI, reason="项目要读训练数据"):
        return self.flows.submit(
            applicant=applicant,
            email="li.si@wuji.tech" if applicant is LI else "new@wuji.tech",
            template_id=template,
            payload=payload if payload is not None else {"bucket": BUCKET, "hours": 2},
            reason=reason,
        )

    def run(self, template="ali-data", payload=None, applicant=LI):
        ticket = self.submit(template, payload, applicant)
        self.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        return self.flows.sync(ticket["id"], force=True)

    def stored(self):
        return (self.dir / "tickets.json").read_text(encoding="utf-8")

    def key(self, ticket):
        """审批评论里那条查看地址的密钥。凭证的唯一出口。"""
        return view_key(self.feishu, ticket)

    def opened(self, ticket, key=None, who=""):
        """像使用方那样打开链接，拿到凭证 dict。"""
        _, cred = self.flows.view_credential(
            ticket["id"], self.key(ticket) if key is None else key, who=who
        )
        return cred


WINDOW = {"not_before": 1_800_000_000.0, "expire": 1_800_003_600.0}


def _cond_keys(stmt):
    return set((stmt.get("Condition") or {}).keys())


class PolicyStructureTests(unittest.TestCase):
    """build_policy 的形状。两朵云共用一份逻辑，所以两边都要过同一组断言。"""

    def build(self, platform="aliyun", **kw):
        kw.setdefault("caps", ["list", "download"])
        return grants.build_policy(platform, BUCKET, **{**WINDOW, **kw})

    def test_bucket_info_statement_is_first_bucket_level_and_never_prefixed(self):
        """线上事故「凭证发了但什么也干不了」的根因：桶信息语句被叠了 oss:Prefix。"""
        for platform, spec in DIALECTS:
            doc = self.build(platform, prefix="team/data/")
            first = doc["Statement"][0]
            self.assertEqual(first["Effect"], "Allow", platform)
            self.assertEqual(first["Action"], list(spec.bucket_actions), platform)
            # 桶级资源，不是 bucket/*：桶级请求不带 prefix 参数
            self.assertEqual(first["Resource"], [spec.bucket_arn(BUCKET)], platform)
            self.assertNotIn("/", first["Resource"][0].rsplit(":", 1)[-1], platform)
            self.assertNotIn("StringLike", _cond_keys(first), platform)
            self.assertNotIn(spec.prefix_key, json.dumps(first, ensure_ascii=False), platform)

    def test_any_non_empty_caps_gets_bucket_info(self):
        """只勾「上传」的使用方同样要探桶：caps 非空就给桶信息，一条都不能少。"""
        for size in (1, 2, 3):
            for caps in itertools.combinations(grants.CAPS, size):
                for platform, spec in DIALECTS:
                    doc = self.build(platform, caps=list(caps))
                    actions = [s["Action"] for s in doc["Statement"]]
                    self.assertIn(list(spec.bucket_actions), actions, (platform, caps))

    def test_every_allow_statement_carries_the_time_window(self):
        for platform, spec in DIALECTS:
            doc = self.build(platform, caps=list(grants.CAPS), prefix="team/")
            allows = [s for s in doc["Statement"] if s["Effect"] == "Allow"]
            self.assertEqual(len(allows), 4, platform)  # 桶信息 + list + download + write
            for stmt in allows:
                cond = stmt["Condition"]
                self.assertEqual(
                    cond["DateGreaterThan"],
                    {spec.time_key: spec.time_value(WINDOW["not_before"])},
                    (platform, stmt["Action"]),
                )
                self.assertEqual(
                    cond["DateLessThan"],
                    {spec.time_key: spec.time_value(WINDOW["expire"])},
                    (platform, stmt["Action"]),
                )

    def test_trailing_deny_is_unconditional(self):
        """兜底 Deny 不叠时间窗：叠上去的话，凭证一过期这条 Deny 也失效了。"""
        for platform, spec in DIALECTS:
            doc = self.build(platform, caps=list(grants.CAPS))
            last = doc["Statement"][-1]
            self.assertEqual(last["Effect"], "Deny", platform)
            self.assertEqual(last["Action"], list(spec.deny_actions), platform)
            self.assertEqual(last["Resource"], [spec.bucket_arn("*")], platform)
            self.assertNotIn("Condition", last, platform)
            self.assertTrue(any("Delete" in a for a in last["Action"]), platform)

    def test_version_only_on_aliyun(self):
        self.assertEqual(self.build("aliyun")["Version"], "1")
        self.assertNotIn("Version", self.build("volcano"))
        # 时间写法也是两套：阿里带 +08:00，火山发 UTC 的 Z
        ali = json.dumps(self.build("aliyun"), ensure_ascii=False)
        volc = json.dumps(self.build("volcano"), ensure_ascii=False)
        self.assertIn("+08:00", ali)
        self.assertNotIn("+08:00", volc)
        self.assertRegex(volc, r"\d{2}:\d{2}:\d{2}Z")

    def test_list_is_bucket_level_with_prefix_condition(self):
        for platform, spec in DIALECTS:
            doc = self.build(platform, caps=["list"], prefix="team/data")
            listing = next(s for s in doc["Statement"] if s["Action"] == list(spec.list_actions))
            self.assertEqual(listing["Resource"], [spec.bucket_arn(BUCKET)], platform)
            self.assertEqual(
                listing["Condition"]["StringLike"],
                {spec.prefix_key: ["team/data/", "team/data/*"]},
                platform,
            )
            # 整桶申请时不叠前缀条件（叠了会把整桶申请也卡死）
            whole = self.build(platform, caps=["list"])
            other = next(s for s in whole["Statement"] if s["Action"] == list(spec.list_actions))
            self.assertNotIn("StringLike", _cond_keys(other), platform)

    def test_download_and_write_are_object_level_and_scoped_to_prefix(self):
        for platform, spec in DIALECTS:
            doc = self.build(platform, caps=["download", "write"], prefix="team/data")
            for actions in (spec.download_actions, spec.write_actions):
                stmt = next(s for s in doc["Statement"] if s["Action"] == list(actions))
                self.assertEqual(
                    stmt["Resource"], [f"{spec.bucket_arn(BUCKET)}/team/data/*"], platform
                )
                # 对象级 ARN 已经带了前缀，不再叠前缀条件
                self.assertNotIn("StringLike", _cond_keys(stmt), platform)

    def test_list_does_not_grant_download_and_write_does_not_grant_delete(self):
        for platform in ("aliyun", "volcano"):
            only_list = json.dumps(self.build(platform, caps=["list"]), ensure_ascii=False)
            self.assertNotIn("GetObject", only_list, platform)
            writing = self.build(platform, caps=["write"])
            allowed = [
                a for s in writing["Statement"] if s["Effect"] == "Allow" for a in s["Action"]
            ]
            self.assertFalse([a for a in allowed if "Delete" in a], (platform, allowed))
            # 写权限不等于能把桶改成公开；读 ACL（属于桶信息）是允许的
            self.assertFalse([a for a in allowed if "PutBucketAcl" in a], (platform, allowed))
            self.assertFalse([a for a in allowed if "PutObjectAcl" in a], (platform, allowed))

    def test_source_ips_deduped_and_sorted(self):
        for platform, spec in DIALECTS:
            doc = self.build(platform, source_ips=["2.2.2.2", "1.1.1.1", "2.2.2.2"])
            for stmt in doc["Statement"]:
                if stmt["Effect"] != "Allow":
                    continue
                self.assertEqual(
                    stmt["Condition"]["IpAddress"], {spec.ip_key: ["1.1.1.1", "2.2.2.2"]}, platform
                )

    def test_bad_inputs_refused(self):
        with self.assertRaises(grants.GrantError):  # 生效不早于到期
            grants.build_policy("aliyun", BUCKET, caps=["list"], not_before=100.0, expire=100.0)
        with self.assertRaises(grants.GrantError):
            grants.build_policy("aliyun", BUCKET, caps=["list"], not_before=200.0, expire=100.0)
        with self.assertRaises(grants.GrantError):  # 空能力集
            self.build(caps=[])
        with self.assertRaises(grants.GrantError):
            self.build(caps=["admin"])
        # 不认识的云：方言表搬到 platforms.py 之后由它来拒。两者同属 DeliveryError，
        # HTTP 层的处理一样，这里断言更精确的那个
        with self.assertRaises(platforms.PlatformError):
            grants.build_policy("aws", BUCKET, caps=["list"], **WINDOW)
        for bucket in ("a", "has space", "x" * 70, "", "-lead", "trail-", "a_b"):
            with self.assertRaises(grants.GrantError, msg=bucket):
                grants.build_policy("aliyun", bucket, caps=["list"], **WINDOW)
        # 大小写按 OSS 规范归一，不报错（模板那层的 _BUCKET_NAME 已经禁掉了大写）
        self.assertEqual(grants.check_bucket("Wuji-Data"), "wuji-data")
        for prefix in ("../etc", "a/../b", "a*", "a?b", "a\\b", "a\nb"):
            with self.assertRaises(grants.GrantError, msg=prefix):
                self.build(prefix=prefix)

    def test_prefix_normalised_so_data_does_not_match_database(self):
        doc = self.build(caps=["download"], prefix="/data")
        stmt = doc["Statement"][-2]
        self.assertTrue(stmt["Resource"][0].endswith("/data/*"), stmt["Resource"])
        self.assertEqual(grants.check_prefix("  a/b  "), "a/b/")
        self.assertEqual(grants.check_prefix(""), "")


class SessionPolicyTests(unittest.TestCase):
    def test_compact_json_round_trips(self):
        doc = grants.build_policy("aliyun", BUCKET, caps=["list"], **WINDOW)
        text = grants.session_policy(doc)
        self.assertEqual(json.loads(text), doc)
        self.assertNotIn(", ", text)  # 紧凑，省字符

    def test_overlong_raises_and_never_truncates(self):
        """截断出来的策略仍是合法 JSON，但少掉的往往正是那条 Deny。"""
        doc = grants.build_policy(
            "aliyun", BUCKET, prefix="x" * 80 + "/" + "y" * 80, caps=list(grants.CAPS), **WINDOW
        )
        doc["Statement"] += doc["Statement"] * 6
        raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        self.assertGreater(len(raw), grants.SESSION_POLICY_MAX)
        with self.assertRaises(grants.GrantError) as ctx:
            grants.session_policy(doc)
        self.assertIn(str(grants.SESSION_POLICY_MAX), str(ctx.exception))

    def test_boundary_just_under_the_limit_passes(self):
        doc = {"Version": "1", "Statement": [{"Effect": "Allow", "Action": "x" * 1980}]}
        self.assertLessEqual(len(grants.session_policy(doc)), grants.SESSION_POLICY_MAX)
        doc["Statement"][0]["Action"] = "x" * 2048
        with self.assertRaises(grants.GrantError):
            grants.session_policy(doc)


class VolcanoSessionPolicyTests(unittest.TestCase):
    """火山 AssumeRole 收到会话策略必须报错：静默忽略 = 把整个角色的范围发出去。"""

    def executor(self):
        def boom(*a, **kw):
            raise AssertionError("不该调用火山接口")

        return VolcanoExecutor(VOLC, volcano.Credentials("AK", "SK"), transport=boom)

    def test_policy_argument_is_refused_before_any_api_call(self):
        doc = grants.build_policy("volcano", TOS_BUCKET, caps=["download"], **WINDOW)
        with self.assertRaises(ProvisionError) as ctx:
            self.executor().assume_role(f"trn:iam::{VOLC}:role/r", "li", 1, policy=doc)
        self.assertIn("长期", str(ctx.exception))
        # 空字典也是「给了会话策略」，同样要拒
        with self.assertRaises(ProvisionError):
            self.executor().assume_role(f"trn:iam::{VOLC}:role/r", "li", 1, policy={})

    def test_without_policy_it_actually_calls_the_api(self):
        calls = []

        def send(url, headers, data=None):
            calls.append(url)
            return 200, {"Result": {"UserMetadata": [{"AccountId": VOLC}]}}

        ex = VolcanoExecutor(VOLC, volcano.Credentials("AK", "SK"), transport=send)
        with self.assertRaises(ProvisionError):  # 返回里没有凭证字段
            ex.assume_role(f"trn:iam::{VOLC}:role/r", "li", 1)
        self.assertTrue(any("AssumeRole" in u for u in calls))


class RoutingTests(unittest.TestCase):
    """长短期不是两种申请，是同一份权限按时长选的两种实现。"""

    def test_twelve_hours_uses_sts_with_a_session_policy(self):
        env = Env()
        done = env.run(payload={"bucket": BUCKET, "hours": catalog_mod.STS_MAX_HOURS})
        self.assertEqual(done["status"], t.DONE)
        sts = [a for a in env.executor.actions if a[0] == "sts"]
        self.assertEqual(len(sts), 1)
        self.assertEqual(sts[0][0:2], ("sts", f"acs:ram::{ACC}:role/panel-data"))
        self.assertEqual(sts[0][3], 12)
        doc = env.executor.session_policies[0]
        self.assertIsNotNone(doc, "STS 不带会话策略 = 把整个角色的范围发出去")
        self.assertEqual(
            doc,
            grants.build_policy(
                "aliyun",
                BUCKET,
                prefix="",
                caps=("list", "download"),
                not_before=env.now[0],
                expire=env.now[0] + 12 * 3600,
            ),
        )
        # 短期凭证没有子账号，发放身份一次都没被用到
        self.assertEqual(env.issuer.actions, [])
        self.assertNotIn("cred_user", done)

    def test_thirteen_hours_goes_long_term_through_the_issuer(self):
        env = Env()
        done = env.run(payload={"bucket": BUCKET, "hours": catalog_mod.STS_MAX_HOURS + 1})
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual([a for a in env.executor.actions if a[0] == "sts"], [])
        issued = [a for a in env.issuer.actions if a[0] == "issue"]
        self.assertEqual(len(issued), 1)
        user = done["cred_user"]
        self.assertEqual(issued[0][1], user)
        self.assertTrue(user.startswith(grants.USER_PREFIX), user)
        # 建号走的是发放身份，不是开通身份
        self.assertEqual([a for a in env.executor.actions if a[0] == "issue"], [])
        self.assertEqual(env.issuer.issued[user]["Statement"][0]["Effect"], "Allow")

    def test_volcano_goes_long_term_even_for_one_hour(self):
        """火山没有角色可扮，一小时也走长期——策略里的时间窗一小时后就拒了。"""
        env = Env()
        done = env.run("volc-data", payload={"bucket": TOS_BUCKET, "hours": 1, "subject": "外部方"})
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(env.executor.session_policies, [])
        self.assertEqual([a[0] for a in env.issuer.actions], ["issue"])
        self.assertEqual(env.issuer_calls, [("volcano", VOLC)])
        doc = env.issuer.issued[done["cred_user"]]
        self.assertNotIn("Version", doc)  # 火山方言
        self.assertTrue(doc["Statement"][0]["Resource"][0].startswith("trn:tos:::"))

    def test_expiry_is_recorded_from_issuance(self):
        env = Env()
        done = env.run(payload={"bucket": BUCKET, "hours": 3})
        self.assertEqual(float(done["expires_at_ts"]), env.now[0] + 3 * 3600)


class IssueFailureTests(unittest.TestCase):
    """发放失败的收尾：云上不能留下没人管的子账号或裸奔的长期密钥。"""

    LONG = {"bucket": BUCKET, "hours": 24}

    def test_issue_failure_cleans_up_the_half_built_user(self):
        env = Env()
        env.issuer.issue_fail = "CreatePolicy 超时"
        failed = env.run(payload=dict(self.LONG))
        self.assertEqual(failed["status"], t.FAILED)
        user = failed["cred_user"]
        # 建到一半失败要把已经建出来的部分清掉，否则重试必撞 EntityAlreadyExists
        self.assertIn(("revoke", user), env.issuer.actions)
        self.assertNotIn("cred_ak_id", failed)
        # 建号失败时一条评论都不该发出去：凭证根本没生成，没有任何东西要交付
        self.assertEqual(env.feishu.texts(), [])

    def test_cleanup_failure_does_not_mask_the_original_error(self):
        env = Env()
        env.issuer.issue_fail = "CreatePolicy 超时"
        env.issuer.revoke_fail = "DeleteUser 也挂了"
        failed = env.run(payload=dict(self.LONG))
        self.assertEqual(failed["status"], t.FAILED)
        self.assertIn("CreatePolicy", failed["events"][-1]["note"])

    def test_undeliverable_link_voids_the_credential(self):
        """查看地址送不出去 = 凭证没人拿得到：留着号也没人用得上，却是一把裸奔的长期密钥。

        密钥只在那条评论里，评论没发出去就**永远没有第二份**（服务端自己也解不开密文），
        所以必须当场把云上那把 AK 连同子账号删掉。
        """
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        user = failed["cred_user"]
        self.assertIn(("revoke", user), env.issuer.actions)
        events = [e["event"] for e in failed["events"]]
        self.assertIn("credential_revoked", events)
        self.assertEqual(failed["cred_ak_id"], "")  # 作废后不再指向一把已删的 AK
        self.assertNotIn("lt-secret", env.stored())
        # 单子不在可查看状态，留下的那段密文也就打不开了（何况密钥根本没发出去）
        with self.assertRaises(FlowError) as ctx:
            env.flows.view_credential(ticket["id"], "A" * 43)
        self.assertEqual(ctx.exception.status, 409)

    def test_retry_reuses_the_reserved_user_name(self):
        """重试不能建出第二个子账号：第一个会永远留在云上没人管。"""
        env = Env()
        env.issuer.issue_fail = "CreateUser 超时"
        failed = env.run(payload=dict(self.LONG))
        first = failed["cred_user"]
        env.issuer.issue_fail = None
        done = env.flows.execute(failed["id"], actor="admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["cred_user"], first)
        self.assertEqual([a[1] for a in env.issuer.actions if a[0] == "issue"], [first])
        reserved = [e for e in done["events"] if e["event"] == "cred_user_reserved"]
        self.assertEqual(len(reserved), 1, "重试不该再占一个新名字")

    def test_user_name_is_recorded_before_the_cloud_call(self):
        """进程在建号途中挂掉时，云上留下的东西还能按这个名字找回来清掉。"""
        env = Env()
        seen = {}

        def crash(user, display_name, policy_doc):
            seen["ticket"] = env.store.get(env.store.all()[-1]["id"])
            seen["user"] = user
            raise RuntimeError("进程在这里挂掉")

        env.issuer.issue_long_term = crash
        failed = env.run(payload=dict(self.LONG))
        self.assertEqual(failed["status"], t.FAILED)
        self.assertEqual(seen["ticket"].get("cred_user"), seen["user"])
        self.assertEqual(seen["ticket"]["status"], t.EXECUTING)

    def test_missing_bucket_in_template_blocks_issuance(self):
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.templates["templates"][0]["buckets"] = [{"name": SHENZHEN, "region": "cn-shenzhen"}]
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertEqual(env.issuer.actions, [])
        self.assertEqual(env.executor.actions, [])

    def test_expired_long_term_credential_is_deleted(self):
        env = Env()
        done = env.run(payload=dict(self.LONG))
        user = done["cred_user"]
        self.assertEqual(env.flows.revoke_expired(), [])
        env.now[0] += 25 * 3600
        self.assertEqual(len(env.flows.revoke_expired()), 1)
        self.assertEqual(env.store.get(done["id"])["status"], t.REVOKED)
        self.assertIn(("revoke", user), env.issuer.actions)
        # 删号走发放身份（开通身份在云上根本没有删号权限）
        self.assertNotIn(("revoke", user), env.executor.actions)

    def test_incomplete_cleanup_keeps_the_ticket_for_the_next_sweep(self):
        env = Env()
        done = env.run(payload=dict(self.LONG))
        env.now[0] += 25 * 3600
        env.issuer.revoke_left = ["删用户：DeleteConflict"]
        self.assertIn("失败", env.flows.revoke_expired()[0])
        self.assertEqual(env.store.get(done["id"])["status"], t.DONE)
        env.issuer.revoke_left = []
        env.flows.revoke_expired()
        self.assertEqual(env.store.get(done["id"])["status"], t.REVOKED)


class OrphanAkTests(unittest.TestCase):
    """清不干净的那把 AK 要留得住线索，并且**不等到期**就被下一轮定时任务收走。"""

    LONG = {"bucket": BUCKET, "hours": 24}

    def undeliverable(self, env):
        """凭证发出来了、评论没送出去 —— 清理这一步也失败。返回那张 FAILED 单。"""
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        env.issuer.revoke_fail = "DeleteUser 也挂了"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        return failed

    def test_leftover_ak_is_recorded_separately_from_cred_ak_id(self):
        """`cred_ak_id` 会被重试成功后的新 AK 覆盖，残留那把的编号得单独记一份 ——
        否则事后只能凭「事件里那行文字」去云上找一把还在生效的长期密钥。"""
        env = Env()
        failed = self.undeliverable(env)
        self.assertEqual(failed["orphan_ak_ids"], ["LTAI-AK-9876"])
        self.assertIn("credential_orphaned", [e["event"] for e in failed["events"]])
        # 残留了就不清空 cred_ak_id：那是唯一还指得到云上那把 AK 的字段
        self.assertEqual(failed["cred_ak_id"], "LTAI-AK-9876")
        self.assertNotIn("lt-secret", env.stored())  # 记的是编号，不是密钥

    def test_failed_ticket_with_a_user_is_reclaimed_without_waiting_for_expiry(self):
        """它的查看密钥只在那条没发出去的评论里，谁都用不了 —— 留着只是一把裸奔的长期 AK。

        按状态只扫 DONE 的话，这种单子永远没人碰，只剩策略里的时间窗兜底。

        收干净之后**状态不动**：这张单子管理员还要能点「重试」，而「已到期回收」没有出边。
        清掉的是 `cred_user` / `cred_ak_id` —— 云上已经没有这个号了，下一轮不该再空转，
        重试时也该重新起名建号，而不是去补一个刚被删掉的子账号。
        """
        env = Env()
        failed = self.undeliverable(env)
        user = failed["cred_user"]
        env.issuer.revoke_fail = None
        lines = env.flows.revoke_expired()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(user, lines[0])
        after = env.store.get(failed["id"])
        self.assertEqual(after["status"], t.FAILED, "推到终态就把管理员的重试出边关掉了")
        self.assertEqual(after.get("cred_user"), "")
        self.assertEqual(after.get("cred_ak_id"), "")
        events = [e["event"] for e in after["events"]]
        self.assertIn("credential_reclaimed", events)
        self.assertNotIn("revoked", events)
        self.assertEqual([a[0] for a in env.issuer.actions].count("revoke"), 2)
        # 申请人从来没拿到过这份凭证，不该收到一条「已到期回收」
        self.assertEqual([e for e, _ in env.notices if e == "revoked"], [])
        # 时钟一步没往前走：这条不是「到期回收」，是「本来就没人拿得到，赶紧收」
        self.assertEqual(env.now[0], 1_800_000_000.0)
        # 收干净了，下一轮不再去删一个已经不存在的用户
        self.assertEqual(env.flows.revoke_expired(), [])
        self.assertEqual([a[0] for a in env.issuer.actions].count("revoke"), 2)

    def test_closed_ticket_with_a_user_is_reclaimed_too(self):
        env = Env()
        failed = self.undeliverable(env)
        user = failed["cred_user"]
        env.flows.close(failed["id"], actor="on_admin", note="送不出去，重新申请")
        self.assertEqual(env.store.get(failed["id"])["status"], t.CLOSED)
        env.issuer.revoke_fail = None
        self.assertEqual(len(env.flows.revoke_expired()), 1)
        after = env.store.get(failed["id"])
        self.assertEqual(after["status"], t.CLOSED, "关掉的单子被回收顺手改成别的状态就对不上账了")
        self.assertEqual(after.get("cred_user"), "")
        self.assertEqual(after.get("cred_ak_id"), "")
        self.assertIn("credential_reclaimed", [e["event"] for e in after["events"]])
        self.assertIn(("revoke", user), env.issuer.actions)
        self.assertEqual(env.flows.revoke_expired(), [])

    def test_reclaim_does_not_race_the_admin_out_of_the_retry_button(self):
        """回收和「重试」在生产上是赛跑：sweep 十分钟一轮，管理员什么时候点没人控制。

        回收把单子推成「已到期回收」的话，输了这场赛跑的管理员就只能重走一轮审批 ——
        REVOKED 没有出边，`execute` 的 expect=[APPROVED, FAILED] 直接 409。
        既有用例都是发完直接 `execute`、**中间不跑 sweep**，所以这条路一直没锁住。
        """
        env = Env()
        failed = self.undeliverable(env)
        first = failed["cred_user"]
        env.issuer.revoke_fail = None
        env.flows.revoke_expired()  # ← 生产上这一轮随时会插在管理员点重试之前
        self.assertEqual(env.store.get(failed["id"])["status"], t.FAILED)
        env.feishu.comment_fail = None  # 飞书恢复了，管理员点「重试」
        done = env.flows.execute(failed["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        # 名字随回收一起清掉了 → 重新起名建号，不会去补一个刚删掉的子账号
        self.assertTrue(done["cred_user"])
        self.assertNotEqual(done["cred_user"], first)
        self.assertIn(("issue", done["cred_user"]), [(a[0], a[1]) for a in env.issuer.actions])
        # 重试真的把凭证交付出去了：使用方打得开那条查看地址
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_failed_ticket_without_a_user_is_left_alone(self):
        """建号之前就失败的单子云上什么都没有。跟着扫只会每轮都去删一个不存在的用户。"""
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.templates["templates"][0]["buckets"] = [{"name": SHENZHEN, "region": "cn-shenzhen"}]
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertFalse(failed.get("cred_user"))
        self.assertEqual(env.flows.revoke_expired(), [])
        self.assertEqual(env.issuer.actions, [])


class ManualRevokeTests(unittest.TestCase):
    """管理员手动作废一份已发出的凭证（链接外泄时用的那个按钮）。"""

    def issued(self, env=None, **payload):
        env = env or Env()
        return env, env.run(payload={"bucket": BUCKET, "hours": 24, **payload})

    def test_revoke_now_kills_the_link_and_deletes_the_cloud_user(self):
        env, done = self.issued()
        key = env.key(done)
        self.assertEqual(env.opened(done, key)["access_key_secret"], "lt-secret")
        after = env.flows.revoke_now(done["id"], actor="on_admin")
        self.assertEqual(after["status"], t.REVOKED)
        self.assertEqual(after["sealed"], {})
        self.assertIn("credential_sealed_dropped", [e["event"] for e in after["events"]])
        self.assertIn(("revoke", done["cred_user"]), env.issuer.actions)
        with self.assertRaises(FlowError) as ctx:
            env.flows.view_credential(done["id"], key)
        self.assertEqual(ctx.exception.status, 404)  # 密文没了：连「这单存在」都不透露

    def test_the_ledger_says_manual_revocation_not_expiry(self):
        """手动作废和到期回收在台账上必须分得开。

        两者都走 `_revoke_credential`，文案却只有「到期」一种的话：一份因为**外泄**被
        紧急掐掉的凭证，事后翻台账看到的是「临时凭证已到期失效」—— 事故复盘时最需要的
        那一行信息（有人动过手、什么时候动的）被写成了例行公事。
        """
        env, done = self.issued()
        revoked = env.flows.revoke_now(done["id"], actor="on_admin")
        note = next(e["note"] for e in revoked["events"] if e["event"] == "revoked")
        self.assertIn("管理员作废", note)
        self.assertNotIn("到期", note)
        # 反面：同一段代码走定时任务那条路时，说的还是「到期」
        env2, other = self.issued()
        env2.now[0] += 100 * 3600
        self.assertEqual(len(env2.flows.revoke_expired()), 1)
        swept = env2.store.get(other["id"])
        self.assertEqual(swept["status"], t.REVOKED)
        note = next(e["note"] for e in swept["events"] if e["event"] == "revoked")
        self.assertIn("到期", note)
        self.assertNotIn("管理员", note)

    def test_link_dies_even_when_the_cloud_call_fails(self):
        """密文先清：云那边失败还能靠定时任务重试，而链接在那之前一直是活的。

        但**接口要如实报失败**。管理员点「作废」唯一的场景就是链接外泄，看到 200 就会
        认为事情办完了 —— 而云上那个子账号和它的长期 AK 还活着。报一个没发生的成功
        是这里最坏的那种谎。
        """
        env, done = self.issued()
        key = env.key(done)
        env.issuer.revoke_fail = "DeleteUser 挂了"
        with self.assertRaises(FlowError) as ctx:
            env.flows.revoke_now(done["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 502)
        # 两件事都要说清楚：链接已经死了（别再急着找它），云上还欠着（要接着收）
        self.assertIn("查看地址已经失效", str(ctx.exception))
        self.assertIn("DeleteUser 挂了", str(ctx.exception), "不写原因就没法判断要不要人工去删")
        after = env.store.get(done["id"])
        self.assertEqual(after["sealed"], {})
        self.assertEqual(after["status"], t.DONE, "云上没删干净就不能记成已作废")
        self.assertTrue(after.get("cred_user"), "号还在云上，字段就得留着指得到它")
        with self.assertRaises(FlowError):
            env.flows.view_credential(done["id"], key)

    def test_a_failed_manual_revoke_is_picked_up_by_the_sweep_without_waiting_for_expiry(self):
        """「定时任务会继续重试」不能是一句空话。

        凭证没到期，按 `expires_at_ts` 永远扫不到 —— 认得出这张单子靠的是
        「密文已经清了（管理员点过作废）而 `cred_user` 还在」。所以这里**一秒都不拨时钟**。
        """
        env, done = self.issued()
        env.issuer.revoke_fail = "DeleteUser 挂了"
        with self.assertRaises(FlowError):
            env.flows.revoke_now(done["id"], actor="on_admin")
        # 云那边还没好：每一轮都重试，状态原地不动，不会误判成「收完了」
        self.assertEqual(len(env.flows.revoke_expired()), 1)
        self.assertEqual(env.store.get(done["id"])["status"], t.DONE)
        env.issuer.revoke_fail = None
        lines = env.flows.revoke_expired()
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(env.store.get(done["id"])["status"], t.REVOKED)
        self.assertEqual(env.now[0], 1_800_000_000.0, "这条不是到期回收，不该靠拨时钟才扫得到")
        self.assertEqual(env.flows.revoke_expired(), [])

    def test_an_intact_credential_is_not_swept_before_it_expires(self):
        """上面那条判据的反面：正常发出去、没人动过的凭证，密文还在 —— 不准提前收。

        少了这一条，「密文没了但 cred_user 还在」很容易被写宽成「DONE 且 cred_user 还在」，
        于是每张长期凭证单在发出去的下一轮就被删号，使用方的 AK 当场失效。
        """
        env, done = self.issued()
        self.assertTrue(env.store.get(done["id"])["sealed"].get("ciphertext"))
        self.assertEqual(env.flows.revoke_expired(), [])
        self.assertEqual([a for a in env.issuer.actions if a[0] == "revoke"], [])
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_revoke_now_is_idempotent_and_refuses_the_wrong_tickets(self):
        env, done = self.issued()
        env.flows.revoke_now(done["id"], actor="on_admin")
        for label, ticket_id in (
            ("已经作废过", done["id"]),
            ("不是凭证单", env.run("ecs-box", {"spec": "4 核 8G", "until": "2027-02-14"})["id"]),
            ("还没发出凭证", env.submit(payload={"bucket": BUCKET, "hours": 2})["id"]),
        ):
            with self.assertRaises(FlowError, msg=label) as ctx:
                env.flows.revoke_now(ticket_id, actor="on_admin")
            self.assertEqual(ctx.exception.status, 409, label)

    def test_short_term_credential_has_nothing_to_delete_but_still_loses_its_link(self):
        """STS 到点自灭，云上没有残留可删 —— 但外泄的链接必须当场失效。"""
        env, done = self.issued(hours=2)
        key = env.key(done)
        self.assertFalse(done.get("cred_user"))
        after = env.flows.revoke_now(done["id"], actor="on_admin")
        self.assertEqual(after["status"], t.REVOKED)
        self.assertEqual([a for a in env.issuer.actions if a[0] == "revoke"], [])
        with self.assertRaises(FlowError):
            env.flows.view_credential(done["id"], key)


class UndeliveredNeverReachesATerminalStateTests(unittest.TestCase):
    """贯穿凭证清理的一条不变式：**没送达的单子永不进终态**。

    REVOKED 没有出边 —— 推进去就等于把「重试」那条出边焊死，申请人只能重走一轮审批。
    而 `_revoke_credential` 的 `not user` 早返回是这条不变式上最后一个缺口：它不看状态，
    一律 `_mark_revoked`。两条真实路径踩得到（下面两个用例各钉一条）。
    """

    ADMIN = requests_api.Caller(
        union_id="on_admin",
        name="管理员",
        email="admin@wuji.tech",
        open_id="ou_admin",
        user_id="u_admin",
        admin=True,
    )

    LONG = {"bucket": BUCKET, "hours": 24}

    def revoke_button(self, ticket):
        """管理员在面板上看到的那个「作废凭证」按钮，亮不亮。"""
        return requests_api.ticket_view(ticket, viewer=self.ADMIN)["actions"]["revoke"]

    def sweep_with_stale_snapshot(self, env, stale):
        """跑一轮 sweep，但让它开头那次 `store.all()` 拿到 `stale` 这份旧快照。

        **不能**直接 `patch(store.all, return_value=stale)`：`TicketStore.get` 就是拿
        `all()` 过滤出来的，一起盖掉的话「删之前复核一次状态」读到的也是这份快照 ——
        复核永远判「没变」，这条用例就自己把自己测绿了。所以只盖第一次。
        """
        real = env.store.all
        seen = []

        def once():
            seen.append(1)
            return stale if len(seen) == 1 else real()

        with mock.patch.object(env.store, "all", side_effect=once):
            return env.flows.revoke_expired()

    def failed_before_issuing(self, env):
        """签发**之前**就炸的单子：云上什么都没建出来，`cred_user` 从来没写过，也没有密文。

        造法是提交之后把模板里的桶换掉 —— 审批通过、开始执行时校验不过。
        """
        ticket = env.submit(payload=dict(self.LONG))
        env.templates["templates"][0]["buckets"] = [{"name": SHENZHEN, "region": "cn-shenzhen"}]
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertFalse(failed.get("cred_user"))
        self.assertFalse((failed.get("sealed") or {}).get("ciphertext"))
        self.assertEqual(env.issuer.actions, [], "根本没走到签发")
        return failed

    def test_a_ticket_that_never_issued_anything_cannot_be_revoked(self):
        """管理员对一张「签发前就失败」的单子点「作废凭证」。

        没有这道闸的话：单子被推成「已到期回收」，台账写「临时凭证已到期失效」，还给申请人
        推一张「已到期收回」的卡 —— 而这张单子**从来没发出过凭证**。更要命的是 `retry`
        要求 `status == FAILED`，重试按钮就此消失，只能重走一轮审批。
        """
        env = Env()
        failed = self.failed_before_issuing(env)
        self.assertFalse(self.revoke_button(failed), "没东西可作废，按钮就不该亮")
        with self.assertRaises(FlowError) as ctx:
            env.flows.revoke_now(failed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)

        after = env.store.get(failed["id"])
        self.assertEqual(after["status"], t.FAILED, "从没发出过凭证的单子不能被记成「已回收」")
        events = [e["event"] for e in after["events"]]
        self.assertNotIn("revoked", events, events)
        self.assertNotIn("credential_sealed_dropped", events, "本来就没有密文可删")
        # 申请人什么都没拿到过，不该收到一张「已到期收回」的卡
        self.assertEqual([e for e, _ in env.notices if e == "revoked"], [])
        self.assertEqual(env.issuer.actions, [], "云上没有这个号，别去删")

        # 重试那条出边还在，而且真的走得通（模板改回去 = 线上把配置修好了）
        self.assertTrue(requests_api.ticket_view(after, viewer=self.ADMIN)["actions"]["retry"])
        env.templates["templates"][0]["buckets"] = json.loads(json.dumps(TEMPLATES))["templates"][
            0
        ]["buckets"]
        done = env.flows.execute(failed["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_clicking_revoke_again_after_the_sweep_cleaned_up_does_not_close_the_ticket(self):
        """定时任务已经收干净了（`credential_reclaimed`，留在 FAILED），按钮还亮着，再点一次。

        第二次点落进的就是 `not user` 那条早返回 —— 它一律推终态的话，等于**回收本身**
        把管理员的重试出边关掉了，而且是在第一轮已经正确处理完之后。

        收干净 = `cred_user`/`cred_ak_id`/`sealed` 三样一起清。密文也得清，否则
        `revoke_now` 那道「没东西可作废」的闸和 `actions.revoke` 的同款条件都还为真：
        按钮永远亮着，点了是空转，前端还会显示成「已作废」而单子其实停在 FAILED。
        留着它也没有任何价值 —— 失败/已关闭的凭证单密文必然**没送达过**（贴评论是
        `_offer_credential` 的最后一步，贴成了就是 DONE），而且子账号刚被这轮删掉。
        """
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"  # 凭证发出来了、评论没送出去
        env.issuer.revoke_fail = "DeleteUser 也挂了"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertTrue(failed.get("cred_user"))

        env.issuer.revoke_fail = None
        env.flows.revoke_expired()  # B2：删号，留在 FAILED，清掉 cred_user
        cleaned = env.store.get(failed["id"])
        self.assertEqual(cleaned["status"], t.FAILED)
        self.assertEqual(cleaned.get("cred_user"), "")
        self.assertEqual(cleaned.get("cred_ak_id"), "")
        self.assertEqual(cleaned.get("sealed"), {}, "密文指向一个云上已经不存在的凭证了")
        self.assertIn("credential_reclaimed", [e["event"] for e in cleaned["events"]])
        revokes = [a for a in env.issuer.actions if a[0] == "revoke"]

        # 收干净了，按钮就该灭 —— 亮着的话管理员点了是空转，前端还显示成「已作废」
        self.assertFalse(self.revoke_button(cleaned))
        with self.assertRaises(FlowError) as ctx:
            env.flows.revoke_now(failed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)

        after = env.store.get(failed["id"])
        self.assertEqual(after["status"], t.FAILED, "第二次点把第一轮的成果抹成了终态")
        events = [e["event"] for e in after["events"]]
        self.assertNotIn("revoked", events, events)
        self.assertEqual(events, [e["event"] for e in cleaned["events"]], "空转还往台账里记了一笔")
        self.assertEqual([e for e, _ in env.notices if e == "revoked"], [])
        self.assertEqual([a for a in env.issuer.actions if a[0] == "revoke"], revokes)
        # 重试那条出边还在
        self.assertTrue(requests_api.ticket_view(after, viewer=self.ADMIN)["actions"]["retry"])
        env.feishu.comment_fail = None
        self.assertEqual(env.flows.execute(failed["id"], actor="on_admin")["status"], t.DONE)

    def test_killing_the_link_of_a_short_term_credential_that_never_arrived(self):
        """短期（STS）凭证签发并加密存好了，但那条审批评论没送出去 —— 单子落 FAILED。

        云上没有残留可删（STS 到点自灭，`cred_user` 从来没有过），**但密文在**，所以
        「作废凭证」按钮是该亮的：那团密文就是这单唯一还能被打开的东西，管理员掐掉它有意义。
        于是这条路会一直走进 `_revoke_credential` 的 `not user` 早返回 —— 上面两条被
        「没东西可作废」那道 409 闸挡在门外，只有这条真的走得到，它是那个早返回唯一的锁。

        没有 `not done` 那道守卫的话：链接是掐掉了，但单子同时被推成「已到期回收」、
        台账写「临时凭证已作废失效」、还给申请人推一张「已收回」的卡 —— 而他**从来没
        拿到过**这份凭证；更要命的是 retry 要求 `status == FAILED`，重试按钮就此消失。
        """
        env = Env()
        ticket = env.submit(payload={"bucket": BUCKET, "hours": 2})
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertFalse(failed.get("cred_user"), "STS 不建子账号")
        self.assertTrue(failed["sealed"].get("ciphertext"))
        self.assertTrue(self.revoke_button(failed), "密文还在，掐链接这件事是有意义的")

        after = env.flows.revoke_now(failed["id"], actor="on_admin")

        self.assertEqual(after["sealed"], {}, "链接必须当场失效")
        self.assertEqual(after["status"], t.FAILED, "从没送达过的单子不能被推进终态")
        events = [e["event"] for e in after["events"]]
        self.assertIn("credential_sealed_dropped", events)
        self.assertNotIn("revoked", events, events)
        self.assertEqual([e for e, _ in env.notices if e == "revoked"], [])
        self.assertEqual(env.issuer.actions, [], "STS 云上没有残留，别去删")
        # 清干净了：按钮灭掉，再点是 409
        self.assertFalse(self.revoke_button(after))
        with self.assertRaises(FlowError) as ctx:
            env.flows.revoke_now(failed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)
        # 重试那条出边还在，而且真的走得通（飞书恢复了）
        self.assertTrue(requests_api.ticket_view(after, viewer=self.ADMIN)["actions"]["retry"])
        env.feishu.comment_fail = None
        done = env.flows.execute(failed["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(env.opened(done)["access_key_secret"], "sts-secret")

    def test_the_sweep_rechecks_the_status_before_it_touches_the_cloud(self):
        """`revoke_expired` 先 `store.all()` 拿快照、再逐张删；而失败单现在保持可重试 ——
        管理员正好在这个窗口里点了重试的话，**刚发出去的新 AK 会被这一轮 sweep 删掉**。

        重试复用同一个子账号名（`cred_user` 还在），所以删的就是那个刚重新发出去的号。
        后面那次 `update(expect=...)` 又因状态已变被静静吞掉 —— sweep 报成功、单子还是
        DONE、使用方手上的 AK 当场失效，台账上一点痕迹都没有。
        """
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        env.issuer.revoke_fail = "DeleteUser 也挂了"
        failed = env.flows.sync(ticket["id"], force=True)
        user = failed["cred_user"]

        env.issuer.revoke_fail = None
        stale = env.store.all()  # ← 定时任务这一轮拿到的快照：还是 FAILED
        env.feishu.comment_fail = None
        done = env.flows.execute(failed["id"], actor="on_admin")  # 管理员抢在删号之前点了重试
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["cred_user"], user, "重试复用同一个子账号名，所以删的就是它")
        before = [a for a in env.issuer.actions if a[0] == "revoke"]

        lines = self.sweep_with_stale_snapshot(env, stale)

        self.assertEqual(len(lines), 1, lines)
        self.assertIn("状态已变", lines[0])
        self.assertEqual([a for a in env.issuer.actions if a[0] == "revoke"], before, "动了云")
        after = env.store.get(failed["id"])
        self.assertEqual(after["status"], t.DONE)
        self.assertEqual(after["cred_user"], user)
        # 最要紧的那条：使用方刚拿到的凭证还打得开
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_a_ticket_that_is_gone_by_the_time_the_sweep_reaches_it_is_skipped(self):
        """同一个窗口的另一种结局：单子在快照之后被删了。不能因为它而中断这一轮。"""
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        env.issuer.revoke_fail = "DeleteUser 也挂了"
        failed = env.flows.sync(ticket["id"], force=True)
        env.issuer.revoke_fail = None
        stale = env.store.all()
        before = [a for a in env.issuer.actions if a[0] == "revoke"]

        with mock.patch.object(env.store, "get", side_effect=t.TicketError("没有这张申请单")):
            lines = self.sweep_with_stale_snapshot(env, stale)

        self.assertEqual(len(lines), 1, lines)
        self.assertIn(failed["id"], lines[0])
        self.assertEqual([a for a in env.issuer.actions if a[0] == "revoke"], before)


class ResourceTicketTests(unittest.TestCase):
    """资源开通：面板只走审批 + 记台账，一行云都不写。"""

    SPEC = "4 核 8G / 100G 云盘 / 杭州"
    #: Env 的时钟钉在 1_800_000_000（2027-01-15 北京时间），+30 天
    UNTIL = "2027-02-14"

    def approved(self, env, payload=None, template="ecs-box"):
        return env.run(template, payload=payload or {"spec": self.SPEC, "until": self.UNTIL})

    def test_stops_at_fulfilling_without_touching_any_executor(self):
        env = Env()
        ticket = self.approved(env)
        self.assertEqual(ticket["status"], t.FULFILLING)
        self.assertEqual(env.exec_calls, [], "资源单不该取执行器")
        self.assertEqual(env.issuer_calls, [])
        self.assertEqual(env.executor.actions, [])
        self.assertEqual([e for e, _ in env.notices], ["fulfilling"])
        self.assertEqual(ticket["events"][-1]["event"], "await_fulfil")
        # 还没有任何资源存在，所以既没有完成时间也没有到期时间
        self.assertNotIn("done_at_ts", ticket)
        self.assertNotIn("expires_at_ts", ticket)

    def test_fulfil_requires_a_note_and_starts_the_clock(self):
        env = Env()
        ticket = self.approved(env)
        for empty in ("", "   ", None):
            with self.assertRaises(FlowError, msg=repr(empty)):
                env.flows.fulfil(ticket["id"], actor="on_admin", note=empty)
        self.assertEqual(env.store.get(ticket["id"])["status"], t.FULFILLING)
        env.now[0] += 3 * 86400  # 管理员三天后才建好
        done = env.flows.fulfil(ticket["id"], actor="on_admin", note="i-abc 4C8G 杭州 h")
        self.assertEqual(done["status"], t.DONE)
        self.assertIn("i-abc", done["result"])
        # 到期时间从登记那一刻起算，不是从审批通过起算
        self.assertEqual(float(done["expires_at_ts"]), env.now[0] + 30 * 86400)
        self.assertEqual([e for e, _ in env.notices], ["fulfilling", "done"])

    def test_fulfil_only_applies_to_resource_tickets_and_only_once(self):
        env = Env()
        ticket = self.approved(env)
        env.flows.fulfil(ticket["id"], actor="on_admin", note="i-abc")
        with self.assertRaises(t.TicketError):  # 已经登记过
            env.flows.fulfil(ticket["id"], actor="on_admin", note="i-def")
        cred = env.run(payload={"bucket": BUCKET, "hours": 2})
        with self.assertRaises(FlowError):
            env.flows.fulfil(cred["id"], actor="on_admin", note="i-abc")

    def test_expired_resource_is_reminded_but_never_reclaimed(self):
        """删机器这种事不能由定时任务替人决定。"""
        env = Env()
        ticket = self.approved(env, {"spec": self.SPEC, "until": "2027-01-20"})  # +5 天
        done = env.flows.fulfil(ticket["id"], actor="on_admin", note="i-abc")
        env.notices.clear()
        env.now[0] += 3 * 86400
        self.assertEqual(len(env.flows.remind_expiring(days=3)), 1)
        self.assertEqual([e for e, _ in env.notices], ["expiring"])
        env.now[0] += 5 * 86400  # 已经过期
        self.assertEqual(env.flows.revoke_expired(), [])
        self.assertEqual(env.store.get(done["id"])["status"], t.DONE)
        self.assertEqual(env.exec_calls, [])

    def test_spec_and_days_bounds(self):
        env = Env()
        for spec in ("", "x", " ", "长" * (catalog_mod.SPEC_MAX + 1)):
            with self.assertRaises(FlowError, msg=repr(spec)):
                env.submit("ecs-box", {"spec": spec, "days": 30})
        for days in (0, 91, -1, True, "30", 1.5):
            with self.assertRaises(FlowError, msg=repr(days)):
                env.submit("ecs-box", {"spec": self.SPEC, "days": days})
        with self.assertRaises(FlowError):  # 补充说明也有上限
            env.submit(
                "ecs-box",
                {"spec": self.SPEC, "days": 30, "detail": "长" * (catalog_mod.SPEC_MAX + 1)},
            )
        # 不限天数的模板：不填天数也能提交，且不会有到期时间
        free = env.submit("rds-free", {"spec": "MySQL 8.0 4C8G"})
        self.assertEqual(free["status"], t.PENDING)
        self.assertEqual(free["payload"]["days"], 0)

    def test_resource_is_requestable_without_a_sub_account(self):
        """面板不建资源，也不要求申请人先有子账号——拿这个当门槛只会把提需求的人挡在外面。"""
        env = Env()
        opts = {o["id"]: o for o in env.flows.options(NEW.union_id)}
        self.assertEqual(opts["ecs-box"]["state"], "available")
        self.assertTrue(opts["ecs-box"]["available"])
        self.assertEqual(opts["ecs-box"]["max_days"], 90)

    def test_unlimited_resource_has_no_expiry_after_fulfil(self):
        env = Env()
        ticket = env.run("rds-free", payload={"spec": "MySQL 8.0 4C8G"})
        self.assertEqual(ticket["status"], t.FULFILLING)
        done = env.flows.fulfil(ticket["id"], actor="on_admin", note="rm-abc 杭州")
        self.assertEqual(done["status"], t.DONE)
        self.assertNotIn("expires_at_ts", done)
        self.assertEqual(env.flows.revoke_expired(), [])


class IssuerGateTests(unittest.TestCase):
    """发放身份没配时，能提前挡住的就别让人白提一轮审批。"""

    def env(self, ready):
        return Env(issuer_ready=lambda platform, account: ready)

    def test_long_term_only_template_is_greyed_out(self):
        opts = {o["id"]: o for o in self.env(False).flows.options("on_li")}
        self.assertEqual(opts["volc-data"]["state"], "unavailable")
        self.assertIn("发放身份", opts["volc-data"]["unavailable_reason"])
        # 配了角色的模板不置灰：12 小时以内的申请照样能办
        self.assertEqual(opts["ali-data"]["state"], "available")
        self.assertTrue(opts["ali-data"]["available"])

    def test_over_twelve_hours_is_refused_at_submit_time(self):
        env = self.env(False)
        with self.assertRaises(FlowError) as ctx:
            env.submit(payload={"bucket": BUCKET, "hours": catalog_mod.STS_MAX_HOURS + 1})
        self.assertIn("12", str(ctx.exception))
        with self.assertRaises(FlowError):
            env.submit("volc-data", {"bucket": TOS_BUCKET, "hours": 1})
        self.assertEqual(env.submit(payload={"bucket": BUCKET, "hours": 12})["status"], t.PENDING)

    def test_everything_passes_when_the_issuer_is_configured(self):
        env = self.env(True)
        opts = {o["id"]: o for o in env.flows.options("on_li")}
        self.assertEqual(opts["volc-data"]["state"], "available")
        self.assertEqual(env.submit(payload={"bucket": BUCKET, "hours": 100})["status"], t.PENDING)


class ValidationTests(unittest.TestCase):
    def test_bucket_must_be_on_the_template_whitelist(self):
        env = Env()
        for bucket in ("", "some-other-bucket", TOS_BUCKET):
            with self.assertRaises(FlowError, msg=bucket):
                env.submit(payload={"bucket": bucket, "hours": 2})

    def test_prefix_rejected_when_template_forbids_it(self):
        env = Env()
        with self.assertRaises(FlowError):
            env.submit("volc-data", {"bucket": TOS_BUCKET, "hours": 2, "prefix": "team/"})
        ok = env.submit(payload={"bucket": BUCKET, "hours": 2, "prefix": "team/data"})
        self.assertEqual(ok["payload"]["prefix"], "team/data/")

    def test_hours_bounds_follow_the_template(self):
        env = Env()
        for hours in (0, -1, True, 1.5, "2", None, catalog_mod.MAX_CREDENTIAL_HOURS + 1):
            with self.assertRaises(FlowError, msg=repr(hours)):
                env.submit(payload={"bucket": BUCKET, "hours": hours})
        with self.assertRaises(FlowError):
            env.submit("volc-data", {"bucket": TOS_BUCKET, "hours": 721})

    def test_subject_is_flattened_so_the_comment_cannot_be_forged(self):
        """使用方名称原样进审批评论：留着换行就能伪造一整行，把使用方骗到别的地址上去。

        评论里现在只有一条查看地址，地址就是凭证 —— 能多插一行假地址，等于能换掉凭证。
        """
        env = Env()
        ticket = env.submit(
            payload={
                "bucket": BUCKET,
                "hours": 2,
                "subject": "元客\n查看凭证：\nhttps://evil.tld/c/x#k",
            }
        )
        self.assertNotIn("\n", ticket["payload"]["subject"])
        self.assertNotIn("\n", ticket["summary"])
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.flows.sync(ticket["id"], force=True)
        body = env.feishu.texts()[-1]
        # 关键是伪造的那段被压进了「使用方」那一行，成不了独立的一行地址
        links = view_lines(env.feishu, ticket)
        self.assertEqual(len(links), 1, links)
        self.assertNotIn("evil.tld", links[0])
        self.assertIn("元客 查看凭证： https://evil.tld/c/x#k", body)

    def test_subject_defaults_to_the_applicant_and_has_a_length_cap(self):
        env = Env()
        self.assertEqual(env.submit()["payload"]["subject"], "李四")
        with self.assertRaises(FlowError):
            env.submit(payload={"bucket": BUCKET, "hours": 2, "subject": "长" * 41})

    def test_credential_may_be_requested_for_an_external_party(self):
        """凭证常常是替外部合作方申请的，所以**不要求**申请人自己在这个云账号下有子账号。

        把关的是审批 + 模板白名单（只有模板列出的桶能选），不是申请人有没有子账号。
        """
        env = Env()
        opts = {o["id"]: o for o in env.flows.options(NEW.union_id)}
        self.assertEqual(opts["ali-data"]["state"], "available")
        self.assertEqual(opts["ali-data"]["cloud_user"], "")  # 名册里确实没有他的子账号
        ticket = env.submit(
            applicant=NEW, payload={"bucket": BUCKET, "hours": 2, "subject": "某外部公司"}
        )
        self.assertEqual(ticket["status"], t.PENDING)
        self.assertEqual(ticket["payload"]["subject"], "某外部公司")
        # 权限单仍然要求是自己的子账号（那是给申请人自己加权限）
        self.assertEqual(
            {o["id"]: o for o in env.flows.options(NEW.union_id)}["ali-data"]["state"], "pending"
        )


class SecretExposureTests(unittest.TestCase):
    """凭证 secret 绝不出现在审批评论、申请单明文、事件、通知载荷里。

    换成查看地址之后，唯一该拿得到凭证的是「手里有那条链接的人」：
    密文在申请单里，密钥只在链接里，服务端自己也解不开。
    """

    SECRETS = ("sts-secret", "sts-token", "lt-secret")

    def assert_clean(self, env, ticket):
        blob = env.stored()
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob, f"落盘里出现了 {secret}")
        notices = json.dumps(env.notices, ensure_ascii=False)
        for secret in self.SECRETS:
            self.assertNotIn(secret, notices, f"通知载荷里出现了 {secret}")
        events = json.dumps(ticket.get("events"), ensure_ascii=False)
        for secret in self.SECRETS:
            self.assertNotIn(secret, events, f"事件里出现了 {secret}")
        self.assertNotIn("secret", set(ticket) - {"cred_user", "cred_ak_id"})
        # 评论里也不能有：它会永远留在飞书的审批记录里，搜索、导出、交接都翻得到
        for text in env.feishu.texts():
            for secret in self.SECRETS:
                self.assertNotIn(secret, text, f"审批评论里出现了 {secret}")
        # 密钥同理：写进申请单或通知，密文就白加密了
        key = view_key(env.feishu, ticket)
        self.assertNotIn(key, blob, "密钥落盘 = 密文形同虚设")
        self.assertNotIn(key, notices)

    def test_short_term_issuance_leaks_nothing(self):
        env = Env()
        done = env.run(payload={"bucket": BUCKET, "hours": 2})
        self.assert_clean(env, done)
        # 凭证只有「拿着链接打开」这一条出口
        cred = env.opened(done)
        self.assertEqual(
            (cred["access_key_secret"], cred["security_token"]), ("sts-secret", "sts-token")
        )
        self.assertFalse(cred["long_term"])

    def test_long_term_issuance_records_only_the_access_key_id(self):
        env = Env()
        done = env.run(payload={"bucket": BUCKET, "hours": 48})
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")
        self.assertEqual(done["cred_ak_id"], "LTAI-AK-9876")
        self.assertIn("LTAI-AK-9876", env.stored())  # AK id 可以留，用来对账
        self.assert_clean(env, done)
        # 事件里只写 AK 的尾号，不写全量
        note = next(e for e in done["events"] if e["event"] == "credential_issued")["note"]
        self.assertIn("9876", note)
        self.assertNotIn("lt-secret", note)

    def test_credential_carries_the_connection_info_people_actually_need(self):
        """深圳的桶配杭州 endpoint 会被 403，而报错看起来就像「凭证无效」。

        连接信息以前钉在审批评论上，现在只跟着凭证走 —— 那就钉在凭证上。
        """
        env = Env()
        done = env.run(payload={"bucket": SHENZHEN, "hours": 2, "prefix": "team/"})
        cred = env.opened(done)
        self.assertEqual(cred["region"], "cn-shenzhen")
        self.assertEqual(cred["endpoint"], "oss-cn-shenzhen.aliyuncs.com")
        self.assertEqual(cred["bucket_url"], f"{SHENZHEN}.oss-cn-shenzhen.aliyuncs.com")
        self.assertNotIn("oss-oss-", json.dumps(cred, ensure_ascii=False))
        self.assertEqual(cred["scope"], f"oss://{SHENZHEN}/team/")
        # 审批人要能在评论里看清批出去的是什么范围、到什么时候
        body = env.feishu.texts()[-1]
        self.assertIn(f"oss://{SHENZHEN}/team/", body)
        self.assertIn("2 小时", body)

    def test_volcano_credential_uses_the_tos_endpoint(self):
        env = Env()
        done = env.run("volc-data", payload={"bucket": TOS_BUCKET, "hours": 2})
        cred = env.opened(done)
        self.assertEqual(cred["endpoint"], "tos-cn-beijing.volces.com")
        self.assertEqual(cred["bucket_url"], f"{TOS_BUCKET}.tos-cn-beijing.volces.com")
        self.assertTrue(cred["scope"].startswith("tos://"), cred["scope"])
        self.assertIn("tos://", env.feishu.texts()[-1])


class ViewBaseUrlGateTests(unittest.TestCase):
    """没有取件地址就不受理凭证申请 —— 而且要在**提交**那一步拒，不是审批通过之后。

    取件地址是凭证的唯一出口。没配 `DELIVERY_BASE_URL` 时贴一条相对路径 `/c/...` 出去，
    凭证照发、单子照样 DONE，使用方拿到一个点不开的链接 —— 而密钥只在那条评论里，
    我们自己也解不开密文，只能整单重来。所以要在提交那一刻拒，且**一行云都不能调**。
    """

    #: 都是不能拿来发凭证的写法。http + 外网域名尤其要拒：密钥在 fragment 里
    #: 不上网，但取件页和密文本身会在明文 HTTP 上裸奔。
    #:
    #: **本机地址单独一族**：`safe_base_url` 为了本地调试放行它们，而 serve 漏配时会
    #: 回填 `http://localhost:<端口>` —— 于是从「一眼看得出坏了的相对路径」变成一个
    #: 像模像样、但使用方点开是他自己 8765 端口的绝对地址，比原来更难发现。
    BAD = {
        "没配": None,
        "空串": "",
        "http 外网域名": "http://panel.example.com",
        "没有 scheme": "panel.example.com",
        "带 query": "https://panel.example.com/?a=1",
        "带空格": "https://panel.example.com /x",
        "serve 漏配回填的本机地址": "http://localhost:8765",
        "本机回环 IP": "http://127.0.0.1:8765",
        "别的回环 IP": "http://127.1.2.3:8765",
        "本机地址套上 https 也还是本机": "https://localhost:8765",
    }

    def without_base_url(self, value=None):
        """临时把 DELIVERY_BASE_URL 换成一个用不了的值（或直接拿掉）。出了 with 就还原。"""
        if value is None:
            patch = mock.patch.dict(os.environ, {})
            patch.start()
            os.environ.pop(notify_mod.ENV_BASE_URL, None)
            self.addCleanup(patch.stop)
            return contextlib.nullcontext()
        return mock.patch.dict(os.environ, {notify_mod.ENV_BASE_URL: value})

    def test_submit_is_refused_and_nothing_reaches_the_cloud(self):
        for label, value in self.BAD.items():
            env = Env()
            with self.subTest(label), self.without_base_url(value):
                with self.assertRaises(FlowError) as ctx:
                    env.submit(payload={"bucket": BUCKET, "hours": 2})
                self.assertEqual(ctx.exception.status, 503)
                self.assertIn(notify_mod.ENV_BASE_URL, str(ctx.exception))
                # 一行云都没调：连取执行器的工厂都不该被碰过
                self.assertEqual(env.exec_calls, [])
                self.assertEqual(env.issuer_calls, [])
                self.assertEqual(env.executor.actions, [])
                self.assertEqual(env.issuer.actions, [])
                # 也没发起飞书审批、没留下单子 —— 审批人不该收到一张根本办不了的单
                self.assertEqual(env.feishu.instances, {})
                self.assertEqual(env.store.all(), [])

    def test_long_term_route_is_refused_too(self):
        """短期走 STS、长期建子账号。两条路都要挡住 —— 长期那条更糟，会留下子账号和 AK。"""
        env = Env()
        with self.without_base_url():
            for template, payload in (
                ("ali-data", {"bucket": BUCKET, "hours": 100}),
                ("volc-data", {"bucket": TOS_BUCKET, "hours": 1}),
            ):
                with self.assertRaises(FlowError, msg=template) as ctx:
                    env.submit(template, payload)
                self.assertEqual(ctx.exception.status, 503, template)
        self.assertEqual(env.issuer.actions, [])
        self.assertEqual(env.executor.actions, [])

    def test_view_base_is_public_and_says_which_kind_of_bad_it_is(self):
        """`view_base()` 是公开入口（体检页也直接调它判「查看地址」那一项）。

        两种坏法要分得清：没配 / 不是 https 是一句，指向本机是另一句 —— 后者的修法
        不是「去配一个」，而是「把已经配上的那个换成对外地址」。
        """
        from delivery import flows as flows_mod

        with mock.patch.dict(os.environ, {notify_mod.ENV_BASE_URL: "https://panel.example.com/"}):
            self.assertEqual(flows_mod.view_base(), "https://panel.example.com")
        local = ("http://localhost:8765", "http://127.0.0.1", "https://localhost:8765")
        for value in local:
            with self.subTest(value), self.without_base_url(value):
                with self.assertRaises(FlowError) as ctx:
                    flows_mod.view_base()
                self.assertEqual(ctx.exception.status, 503)
                self.assertIn("本机", str(ctx.exception))
        with self.without_base_url():
            with self.assertRaises(FlowError) as ctx:
                flows_mod.view_base()
            self.assertEqual(ctx.exception.status, 503)
            self.assertNotIn("本机", str(ctx.exception))

    def test_an_injected_environment_wins_over_the_process_environment(self):
        """体检页要拿它去查**定时任务那份** EnvironmentFile，而不是面板进程自己的环境。

        恒读 `os.environ` 的话，「查看地址」那一项报的永远是面板自己的配置 —— 一份
        根本没配对外地址的定时任务环境会被判成正常，而这一项存在的全部意义就是发现它。
        缺省（不传）仍旧读进程环境：`_offer_credential` 那几处调用是这么用的。
        """
        from delivery import flows as flows_mod

        other = "https://from-the-timer-unit.example.com"
        with mock.patch.dict(os.environ, {notify_mod.ENV_BASE_URL: "https://panel.example.com"}):
            # 传进来的赢
            self.assertEqual(flows_mod.view_base({notify_mod.ENV_BASE_URL: other}), other)
            # 传进来的是空的 → 报「没配」，绝不悄悄回落到进程环境里那个好地址
            for label, env in (("空 dict", {}), ("有键但空值", {notify_mod.ENV_BASE_URL: ""})):
                with self.subTest(label):
                    with self.assertRaises(FlowError) as ctx:
                        flows_mod.view_base(env)
                    self.assertEqual(ctx.exception.status, 503)
                    self.assertNotIn("panel.example.com", str(ctx.exception))
            # 不传 = 老行为，读进程环境
            self.assertEqual(flows_mod.view_base(), "https://panel.example.com")

    def test_other_kinds_of_request_still_go_through(self):
        """挡的只是凭证申请。资源单不发凭证，没有取件地址照样该能提 —— 一刀切会把面板停掉。"""
        env = Env()
        with self.without_base_url():
            ticket = env.submit("ecs-box", {"spec": "4 核 8G", "until": "2027-02-14"})
        self.assertEqual(ticket["status"], t.PENDING)


class SweepIssuerWiringTests(unittest.TestCase):
    """无人值守那条路（`delivery requests`）用的是不是**发放身份**。

    以前 `_sweep` 构造 Flows 时没传 `issuer=`，发凭证和到期回收就全落到开通身份头上 ——
    而开通身份在云上被故意禁掉了 CreatePolicy / CreateAccessKey / DeleteUser。后果是
    子账号建得出来、策略建不上，清理和到期回收也全失败，留一地没人知道的孤儿 AK。

    这条路在别处完全看不到：所有 Flows 用例都自己显式传 `issuer=`，
    只有 `_sweep` 里那一次构造是无人值守时产线真正跑的那份。
    """

    def setUp(self):
        from delivery import people as people_mod

        self.env = Env()
        self.work = Path(tempfile.mkdtemp())
        ident = self.work / "identity"
        ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(self.env.templates), encoding="utf-8")
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
        # 让 Env 和 _sweep 共用同一个申请单文件：单子在这边造，由 _sweep 那边推进
        self.env.store = t.TicketStore(str(ident / "tickets.json"), clock=lambda: self.env.now[0])
        self.env.flows.store = self.env.store

    def sweep(self):
        """在临时的 identity/ 目录里跑一次 `_sweep`，返回它自己构造出来的那个 Flows。"""
        import argparse

        from delivery import cli_requests
        from delivery.flows import Flows

        env = self.env
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )
        built = []

        class Recording(Flows):
            def __init__(self, **kw):
                super().__init__(**kw)
                built.append(self)

        class Approval(FeishuApproval):
            def __init__(self, config, token):
                super().__init__(config, token, transport=env.feishu)

        def executor_from_env(platform, account, *, issuer=False):
            (env.issuer_calls if issuer else env.exec_calls).append((platform, account))
            return env.issuer if issuer else env.executor

        cwd = Path.cwd()
        os.chdir(self.work)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {"DELIVERY_FEISHU_APP_ID": "cli_x", "DELIVERY_FEISHU_APP_SECRET": "s"},
                ),
                mock.patch("delivery.identity.directory.tenant_token", lambda *a: "tok"),
                mock.patch("delivery.approval.FeishuApproval", Approval),
                mock.patch("delivery.provision.executor_from_env", executor_from_env),
                mock.patch("delivery.flows.Flows", Recording),
                mock.patch("delivery.cli._git_toplevel", lambda path: None),
            ):
                cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(len(built), 1, "_sweep 应该正好构造一个 Flows")
        return built[0]

    def test_sweep_wires_an_issuer_that_is_not_the_executor(self):
        flows = self.sweep()
        self.assertIsNotNone(flows._issuer, "_sweep 没传 issuer=，发凭证会落到开通身份头上")
        issuer = flows._issuer("aliyun", ACC)
        self.assertIsNotNone(issuer)
        self.assertIsNot(
            issuer, flows._executor("aliyun", ACC), "发放身份和开通身份是云上两把不同的 AK"
        )

    def expire(self, ticket_id: str):
        """把这张单子的到期时间挪到真实时间之前，模拟「凭证到期了」。"""
        path = self.work / "identity" / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for ticket in data["tickets"]:
            if ticket["id"] == ticket_id:
                ticket["expires_at_ts"] = time.time() - 60
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_sweep_issues_and_reclaims_through_the_issuer(self):
        """光看 `_issuer` 不为 None 不够：签发和到期回收要真的走到它身上。"""
        env = self.env
        ticket = env.submit(payload={"bucket": BUCKET, "hours": 100})  # 长期 → 建子账号发 AK
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"

        self.sweep()
        done = env.store.get(ticket["id"])
        self.assertEqual(done["status"], t.DONE, done)
        self.assertEqual([a[0] for a in env.issuer.actions], ["issue"])
        self.assertEqual(env.executor.actions, [], "开通身份在云上没有 CreateAccessKey")

        # 到期回收同理：走错身份的话 DeleteUser 会失败，长期 AK 到期后还留在云上。
        # `_sweep` 自己构造的 Flows 用的是真实时钟（没有注入口），所以只能把到期时间挪到过去
        self.expire(ticket["id"])
        self.sweep()
        self.assertEqual([a[0] for a in env.issuer.actions], ["issue", "revoke"])
        self.assertEqual(env.executor.actions, [])
        self.assertEqual(env.store.get(ticket["id"])["status"], t.REVOKED)


if __name__ == "__main__":
    unittest.main()


class ResourceOptionTests(unittest.TestCase):
    """资源模板的选项轴、成本归属、到期日期。

    这几样决定了「申请人能影响什么」—— 答案必须是「只能影响选哪个」，
    而不是「能影响传给云 API 的参数」。
    """

    BASE = {
        "id": "ecs-axes",
        "kind": "resource",
        "platform": "aliyun",
        "account": ACC,
        "title": "ECS",
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
                "label": "系统盘",
                "choices": [
                    {"id": "d40", "label": "40G", "params": {"SystemDisk.Size": "40"}},
                    {
                        "id": "d500",
                        "label": "500G",
                        "params": {"SystemDisk.Size": "500", "SystemDisk.Category": "cloud_auto"},
                    },
                ],
            },
        ],
        "cost_centers": [{"id": "algo", "label": "算法组"}],
    }

    def tpl(self, **over):
        return catalog_mod.parse_template({**self.BASE, **over}, 0)

    def test_choice_params_override_the_template_defaults(self):
        """基础参数是默认值，轴上的选项覆盖它 —— 否则想关掉一个默认值就只能到处重复。"""
        t = self.tpl()
        small = t.resolved_params({"spec": "s", "disk": "d40"})
        self.assertEqual(small["SystemDisk.Category"], "cloud_essd")  # 用了基础参数
        self.assertEqual(small["SystemDisk.Size"], "40")
        big = t.resolved_params({"spec": "l", "disk": "d500"})
        self.assertEqual(big["SystemDisk.Category"], "cloud_auto")  # 被选项覆盖
        self.assertEqual(big["InstanceType"], "ecs.g8i.2xlarge")

    def test_unknown_choice_ids_are_ignored_not_injected(self):
        """申请人传来的值只用来查表。查不到就跳过，绝不能变成参数本身。"""
        t = self.tpl()
        out = t.resolved_params({"spec": "InstanceType=evil", "disk": None, "不存在": "x"})
        self.assertEqual(out, dict(self.BASE["params"]))
        self.assertNotIn("evil", json.dumps(out))

    def test_cloud_params_never_reach_the_frontend(self):
        """镜像、交换机、安全组 ID 是内网拓扑，申请人只该看到「通用 2核8G」这种名字。"""
        public = json.dumps(self.tpl().public(), ensure_ascii=False)
        self.assertNotIn("vsw-1", public)
        self.assertNotIn("params", public)
        self.assertNotIn("ecs.g8i.large", public)
        self.assertIn("规格", public)

    def test_placeholder_in_params_is_refused_at_load(self):
        """占位符长得像正常字符串，能一路传到 RunInstances 才报错 —— 那时审批已经过了。"""
        with self.assertRaises(catalog_mod.CatalogError):
            self.tpl(params={"VSwitchId": "<待填>"})

    def test_cost_center_other_cannot_be_a_listed_choice(self):
        """「其他」写进清单的话，查表就查得到，自填那条分支永远走不到（真踩过）。"""
        with self.assertRaises(catalog_mod.CatalogError):
            self.tpl(cost_centers=[{"id": "other", "label": "其他"}])

    def test_cost_center_other_needs_a_list_to_fall_back_from(self):
        with self.assertRaises(catalog_mod.CatalogError):
            self.tpl(cost_centers=[], cost_center_other=True)

    def test_dangerous_params_are_refused_in_every_choice_not_just_the_base(self):
        """禁用清单要覆盖每个选项的 params，只查基础参数等于没查。"""
        for bad in ({"UserData": "ZWNobw=="}, {"RamRoleName": "admin"}, {"AutoPay": "true"}):
            with self.assertRaises(catalog_mod.CatalogError, msg=str(bad)):
                self.tpl(
                    options=[
                        {
                            "id": "spec",
                            "label": "规格",
                            "choices": [{"id": "s", "label": "小", "params": bad}],
                        }
                    ]
                )

    def test_dependent_axis_is_skipped_when_hidden(self):
        """选了「不要公网 IP」就不该再问计费方式，也不该把计费参数带进去。"""
        t = catalog_mod.parse_template(
            {
                **self.BASE,
                "options": [
                    {
                        "id": "net",
                        "label": "公网带宽",
                        "choices": [
                            {"id": "none", "label": "不要", "params": {"Bw": "0"}},
                            {"id": "m100", "label": "100M", "params": {"Bw": "100"}},
                        ],
                    },
                    {
                        "id": "billing",
                        "label": "公网计费",
                        "hidden_when": {"net": ["none"]},
                        "choices": [
                            {"id": "t", "label": "按流量", "params": {"Charge": "PayByTraffic"}}
                        ],
                    },
                ],
            },
            0,
        )
        off = t.resolved_params({"net": "none", "billing": "t"})
        self.assertEqual(off.get("Bw"), "0")
        self.assertNotIn("Charge", off)  # 隐藏的轴不贡献参数，哪怕前端把值传上来了
        self.assertEqual(t.hidden_axes({"net": "none"}), {"billing"})
        on = t.resolved_params({"net": "m100", "billing": "t"})
        self.assertEqual(on.get("Charge"), "PayByTraffic")
        self.assertEqual(t.hidden_axes({"net": "m100"}), set())

    def test_a_hidden_axis_cannot_hide_another_one(self):
        """「A 隐藏 B、B 隐藏 C」：B 根本没被问，它上面那个值是申请人凭空提交的，
        不能拿它去把 C 也隐藏掉 —— 那样 C 不被校验、参数退回模板默认，
        而审批人看到的单子上压根没有 C 这一行。所以隐藏关系要算到不动点。
        """
        t = catalog_mod.parse_template(
            {
                **self.BASE,
                "options": [
                    {
                        "id": "a",
                        "label": "A",
                        "choices": [
                            {"id": "off", "label": "关", "params": {"A": "0"}},
                            {"id": "on", "label": "开", "params": {"A": "1"}},
                        ],
                    },
                    {
                        "id": "b",
                        "label": "B",
                        "hidden_when": {"a": ["off"]},
                        "choices": [
                            {"id": "x", "label": "X", "params": {"B": "x"}},
                            {"id": "y", "label": "Y", "params": {"B": "y"}},
                        ],
                    },
                    {
                        "id": "c",
                        "label": "C",
                        "hidden_when": {"b": ["x"]},
                        "choices": [{"id": "z", "label": "Z", "params": {"C": "z"}}],
                    },
                ],
            },
            0,
        )
        # a=off → b 被隐藏；b 的那个 "x" 是申请人自己塞的，不作数，c 仍然要被问
        self.assertEqual(t.hidden_axes({"a": "off", "b": "x", "c": "z"}), {"b"})
        params = t.resolved_params({"a": "off", "b": "x", "c": "z"})
        self.assertNotIn("B", params)
        self.assertEqual(params.get("C"), "z", "被隐藏的轴把 C 也隐藏掉了")
        # a=on → b 真的被问了，这时它才有资格隐藏 c
        self.assertEqual(t.hidden_axes({"a": "on", "b": "x", "c": "z"}), {"c"})
        self.assertNotIn("C", t.resolved_params({"a": "on", "b": "x", "c": "z"}))
        self.assertEqual(t.hidden_axes({"a": "on", "b": "y", "c": "z"}), set())

    def test_hidden_when_must_reference_an_earlier_axis(self):
        """引用后面的轴就是循环依赖，前端也没法按顺序显示。"""
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(
                {
                    **self.BASE,
                    "options": [
                        {
                            "id": "a",
                            "label": "A",
                            "hidden_when": {"b": ["x"]},
                            "choices": [{"id": "x", "label": "X", "params": {"K": "1"}}],
                        },
                        {
                            "id": "b",
                            "label": "B",
                            "choices": [{"id": "x", "label": "X", "params": {"K": "2"}}],
                        },
                    ],
                },
                0,
            )

    def test_hidden_when_choice_ids_must_exist(self):
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(
                {
                    **self.BASE,
                    "options": [
                        {
                            "id": "a",
                            "label": "A",
                            "choices": [{"id": "x", "label": "X", "params": {"K": "1"}}],
                        },
                        {
                            "id": "b",
                            "label": "B",
                            "hidden_when": {"a": ["nope"]},
                            "choices": [{"id": "y", "label": "Y", "params": {"K": "2"}}],
                        },
                    ],
                },
                0,
            )

    def test_prepaid_is_refused_anywhere(self):
        """包年包月提交即扣款，而且火山的到期前删不掉。自动开通一律按量付费。"""
        with self.assertRaises(catalog_mod.CatalogError):
            self.tpl(params={"VSwitchId": "vsw-1", "InstanceChargeType": "PrePaid"})

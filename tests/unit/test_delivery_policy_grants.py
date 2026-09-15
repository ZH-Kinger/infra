"""按策略申请权限：策略目录与规则、提交校验、开通前复核、授予与到期撤销、权限列表、审批跳转链接。

重点锁住：禁用清单挡住提权类策略且规则文件只能追加（R3）；只能给自己的子账号（R2）；
开通前策略必须仍在当前目录里且没被禁用；到期撤销不误删原有授权、不让续期变成永久。
数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import people as people_mod
from delivery import policies as pol
from delivery import tickets as t
from delivery.approval import (
    DEFAULT_INSTANCE_URL,
    ApprovalConfig,
    ApprovalError,
    FeishuApproval,
    instance_links,
)
from delivery.clouds import aliyun
from delivery.flows import POLICY_TEMPLATE, FlowError, Flows
from delivery.provision import AliyunExecutor, ProvisionError
from delivery.requests_api import Caller, RequestsApi

from .test_delivery_access_requests import (
    ACC,
    CONFIG,
    LI,
    TEMPLATES,
    FakeFeishu,
    MemberExecutor,
    _roster,
)

OSS_READ = {"type": "System", "name": "AliyunOSSReadOnlyAccess"}
ECS_READ = {"type": "System", "name": "AliyunECSReadOnlyAccess"}
PAI_FULL = {"type": "System", "name": "AliyunPAIFullAccess"}
CUSTOM = {"type": "Custom", "name": "team-data-reader"}


def _directory(*extra):
    items = [
        {
            "type": "System",
            "name": "AliyunOSSReadOnlyAccess",
            "description": "只读访问对象存储服务(OSS)的权限",
        },
        {
            "type": "System",
            "name": "AliyunECSReadOnlyAccess",
            "description": "只读访问云服务器服务(ECS)的权限",
        },
        {
            "type": "System",
            "name": "AliyunPAIFullAccess",
            "description": "管理机器学习平台（PAI）的权限",
        },
        {
            "type": "System",
            "name": "AdministratorAccess",
            "description": "管理所有阿里云资源的权限",
        },
        {"type": "Custom", "name": "team-data-reader", "description": "团队数据只读"},
        *extra,
    ]
    return {
        "captured_at": "2026-09-15T10:00:00+08:00",
        "accounts": [
            {
                "platform": "aliyun",
                "account": ACC,
                "policies": [
                    {**p, "service": pol.service_of(p["name"], p["description"]), "updated": ""}
                    for p in items
                ],
            }
        ],
    }


class PolicyExecutor(MemberExecutor):
    """记录直接授予关系的执行器：attach / detach 会改变 has_policy 的结果。"""

    def __init__(self):
        super().__init__()
        self.attached = set()
        self.fail_attach = None

    def has_policy(self, user, ptype, name):
        return (user, ptype, name) in self.attached

    def attach_policy(self, user, ptype, name):
        if self.fail_attach == name:
            raise ProvisionError(f"AttachPolicyToUser {name} 超时")
        self.actions.append(("attach", user, name))
        self.attached.add((user, ptype, name))

    def detach_policy(self, user, ptype, name):
        self.actions.append(("detach", user, name))
        self.attached.discard((user, ptype, name))


class Env:
    def __init__(self, rules=None, current=None):
        self.dir = Path(tempfile.mkdtemp())
        self.feishu = FakeFeishu()
        self.executor = PolicyExecutor()
        self.snapshot = _directory()
        self.rules = rules or pol.Rules()
        self.current = current
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        self.approval = FeishuApproval(CONFIG, lambda: "tok", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(TEMPLATES),
            approval=lambda: self.approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            policy_snapshot=lambda: self.snapshot,
            policy_rules=lambda: self.rules,
            current_policies=lambda platform, account, user: self.current,
            clock=lambda: self.now[0],
        )

    def payload(self, *policies, days=30, user="lisi"):
        return {
            "platform": "aliyun",
            "account": ACC,
            "cloud_user": user,
            "days": days,
            "policies": list(policies) or [OSS_READ],
        }

    def submit(self, *policies, days=30, user="lisi"):
        return self.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id=POLICY_TEMPLATE,
            payload=self.payload(*policies, days=days, user=user),
            reason="项目需要读取训练数据",
        )

    def grant(self, *policies, days=30):
        ticket = self.submit(*policies, days=days)
        self.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        return self.flows.sync(ticket["id"], force=True)


# ── 规则 ─────────────────────────────────────────────────────────────────


class RulesTests(unittest.TestCase):
    def test_builtin_deny_blocks_privilege_escalation(self):
        rules = pol.Rules()
        for name in (
            "AdministratorAccess",
            "AliyunRAMFullAccess",
            "IAMFullAccess",
            "AliyunSTSAssumeRoleAccess",
            "AliyunBSSFullAccess",
            "AliyunActionTrailFullAccess",
        ):
            self.assertTrue(rules.denied("System", name), name)
        self.assertEqual(rules.denied("System", "AliyunOSSReadOnlyAccess"), "")
        # 通配符不能误伤名字里恰好含 sts / ram 字母的正常策略
        for name in (
            "AliyunCostsReadOnlyAccess",
            "AliyunDataProgramFullAccess",
            "AliyunRAMReadOnlyAccess",
        ):
            self.assertEqual(rules.denied("System", name), "", name)
        self.assertTrue(rules.denied("System", "AliyunRAMAccessAnalyzerFullAccess"))

    def test_rules_file_can_only_add_denies(self):
        rules = pol.parse_rules({"deny": ["*KMSFullAccess"]})
        self.assertTrue(rules.denied("System", "AdministratorAccess"))
        self.assertTrue(rules.denied("System", "AliyunKMSFullAccess"))
        opened = pol.parse_rules({"allow": ["AliyunSTSAssumeRoleAccess"]})
        self.assertEqual(opened.denied("System", "AliyunSTSAssumeRoleAccess"), "")
        self.assertTrue(opened.denied("System", "AdministratorAccess"))

    def test_risk_levels_and_days(self):
        rules = pol.Rules()
        self.assertEqual(rules.risk_of("System", "AliyunOSSReadOnlyAccess"), "low")
        self.assertEqual(rules.risk_of("System", "AliyunPAIFullAccess"), "high")
        self.assertEqual(
            rules.risk_of("System", "AliyunDataWorksAccessingRdsOSSBinlogPolicy"), "medium"
        )
        self.assertEqual(rules.risk_of("Custom", "team-data-reader"), "high")
        self.assertEqual(rules.max_days_of("System", "AliyunOSSReadOnlyAccess"), 180)
        custom = pol.parse_rules({"risk": {"team-data-reader": "low"}, "max_days": {"high": 14}})
        self.assertEqual(custom.risk_of("Custom", "TEAM-DATA-READER"), "low")
        self.assertEqual(custom.max_days_of("System", "AliyunPAIFullAccess"), 14)

    def test_custom_policies_closed_by_default(self):
        # 执行身份自己的策略就是自定义策略：名字看不出能做什么，默认一律不开放
        for rules in (pol.Rules(), pol.parse_rules({})):
            self.assertEqual(rules.denied("Custom", "delivery-executor"), pol.CUSTOM_NOTE)
        named = pol.parse_rules({"allow": ["team-data-reader"]})
        self.assertEqual(named.denied("Custom", "team-data-reader"), "")
        self.assertEqual(named.denied("Custom", "delivery-executor"), pol.CUSTOM_NOTE)
        opened = pol.parse_rules({"allow_custom": True})
        self.assertEqual(opened.denied("Custom", "team-data-reader"), "")

    def test_identity_billing_audit_families_denied_except_readonly(self):
        rules = pol.Rules()
        for name in (
            "AliyunKMSFullAccess",
            "AliyunKMSReadOnlyAccess",
            "AliyunIDaaSEAMFullAccess",
            "AliyunConfigFullAccess",
            "AliyunBSSOrderAccess",
            "AliyunCloudSSOAdministratorAccess",
            "AliyunResourceDirectoryAdministratorAccess",
            "CloudIdentityFullAccess",
            "KMSFullAccess",
            "IAMUserManageAccess",
        ):
            self.assertTrue(rules.denied("System", name), name)
        for name in ("AliyunBSSReadOnlyAccess", "AliyunConfigReadOnlyAccess", "IAMReadOnlyAccess"):
            self.assertEqual(rules.denied("System", name), "", name)

    def test_bad_rules_rejected(self):
        for bad in (
            {"denny": []},
            {"max_days": {"low": 0}},
            {"max_days": {"critical": 3}},
            {"risk": {"x": "extreme"}},
            {"allow_custom": "yes"},
            {"max_per_request": 50},
            {"schema": "other"},
        ):
            with self.assertRaises(pol.PolicyError, msg=bad):
                pol.parse_rules(bad)

    def test_missing_rules_file_uses_builtin(self):
        self.assertEqual(pol.load_rules(str(Path(tempfile.mkdtemp()) / "none.json")), pol.Rules())

    def test_service_parsing(self):
        self.assertEqual(
            pol.service_of("AliyunECSReadOnlyAccess", "只读访问云服务器服务(ECS)的权限"), "ECS"
        )
        self.assertEqual(
            pol.service_of("AliyunPAIFullAccess", "管理机器学习平台（PAI）的权限"), "PAI"
        )
        self.assertEqual(pol.service_of("AliyunOSSFullAccess", ""), "OSS")
        self.assertEqual(pol.service_of("TOSReadOnlyAccess"), "TOS")


class CollectTests(unittest.TestCase):
    def test_failed_collection_keeps_last_good_list_marked_stale(self):
        good = pol.build_snapshot(
            [("aliyun", "ALIYUN", lambda: (ACC, _directory()["accounts"][0]["policies"]))]
        )

        def boom():
            raise aliyun.AliyunError("`ListPolicies` 失败 HTTP 503：busy")

        data = pol.build_snapshot([("aliyun", "ALIYUN", boom)], previous=good)
        entry = data["accounts"][0]
        self.assertTrue(entry["stale"])
        self.assertEqual(entry["account"], ACC)
        self.assertIsNotNone(pol.directory(data, "aliyun", ACC))
        # 另一个凭证来源失败，不能拿这份旧列表盖住本次新采到的同一账号
        fresh = [{"type": "System", "name": "AliyunNewAccess", "description": "", "service": "New"}]
        mixed = pol.build_snapshot(
            [("aliyun", "ALIYUN", lambda: (ACC, fresh)), ("aliyun", "OTHER", boom)], previous=good
        )
        self.assertEqual(pol.directory(mixed, "aliyun", ACC), fresh)
        self.assertFalse(any(a.get("stale") for a in mixed["accounts"]))
        # 连续失败不会把 stale 标记叠进去，也不会丢掉列表
        again = pol.build_snapshot([("aliyun", "ALIYUN", boom)], previous=data)
        self.assertEqual(again["accounts"][0]["policies"], entry["policies"])

    def test_aliyun_collect_normalizes(self):
        items = [
            {
                "PolicyType": "System",
                "PolicyName": "AliyunOSSReadOnlyAccess",
                "Description": "(OSS)",
            },
            {"PolicyType": "Custom", "PolicyName": "训练平台权限", "Description": "团队"},
        ]
        with (
            mock.patch.object(pol.aliyun, "call", lambda *a, **k: {"AccountId": ACC}),
            mock.patch.object(pol.aliyun, "paginate", lambda *a, **k: items),
        ):
            account, got = pol.collect_aliyun(aliyun.Credentials("a", "b"))
        self.assertEqual(account, ACC)
        self.assertEqual([p["name"] for p in got], ["训练平台权限", "AliyunOSSReadOnlyAccess"])
        self.assertEqual(got[1]["service"], "OSS")

    def test_volcano_collect_uses_category_and_skips_service_role(self):
        seen = {}

        def paginate(*a, **k):
            seen.update(k.get("params") or {})
            return [
                {"PolicyType": "System", "PolicyName": "TOSReadOnlyAccess", "Category": "tos"},
                {"PolicyType": "System", "PolicyName": "ServiceRoleX", "IsServiceRolePolicy": 1},
            ]

        users = {"UserMetadata": [{"AccountId": "2000000001"}]}
        with (
            mock.patch.object(pol.volcano, "call", lambda *a, **k: users),
            mock.patch.object(pol.volcano, "paginate", paginate),
        ):
            account, got = pol.collect_volcano(pol.volcano.Credentials("a", "b"))
        self.assertEqual(account, "2000000001")
        self.assertEqual([(p["name"], p["service"]) for p in got], [("TOSReadOnlyAccess", "TOS")])
        self.assertEqual(seen, {"Scope": "All", "WithServiceRolePolicy": "0"})

    def test_failed_account_is_error_not_empty(self):
        def boom():
            raise aliyun.AliyunError(
                "`ListPolicies` 失败 HTTP 403：Forbidden AccessKeyId=LTAIsecret12345"
            )

        data = pol.build_snapshot([("aliyun", "ALIYUN", boom)])
        self.assertIn("error", data["accounts"][0])
        self.assertIsNone(pol.directory(data, "aliyun", "ALIYUN"))
        self.assertNotIn("LTAIsecret12345", json.dumps(data))


# ── 提交 ─────────────────────────────────────────────────────────────────


class SubmitTests(unittest.TestCase):
    def test_submit_builds_synthetic_template(self):
        env = Env()
        ticket = env.submit(ECS_READ, OSS_READ, OSS_READ, days=30)
        self.assertEqual(ticket["kind"], "permission")
        tpl = ticket["template"]
        self.assertEqual(tpl["id"], POLICY_TEMPLATE)
        self.assertEqual(tpl["groups"], [])
        self.assertEqual(
            [p["name"] for p in tpl["policies"]],
            ["AliyunECSReadOnlyAccess", "AliyunOSSReadOnlyAccess"],
        )
        self.assertEqual(tpl["risk"], "low")
        self.assertEqual(tpl["max_days"], 180)
        self.assertEqual(len(ticket["payload"]["policies"]), 2)
        self.assertIn("低风险", ticket["summary"])
        self.assertEqual(ticket["status"], t.PENDING)

    def test_days_limited_by_riskiest_policy(self):
        env = Env()
        with self.assertRaises(FlowError):
            env.submit(OSS_READ, PAI_FULL, days=31)
        self.assertEqual(env.submit(OSS_READ, PAI_FULL, days=30)["template"]["risk"], "high")

    def test_rejections(self):
        env = Env(rules=pol.parse_rules({"max_per_request": 2, "allow": ["team-data-reader"]}))
        cases = [
            ({"policies": [{"type": "System", "name": "AliyunNoSuchAccess"}]}, "没有策略"),
            ({"policies": [{"type": "System", "name": "AdministratorAccess"}]}, "不开放"),
            ({"policies": [{"type": "Custom", "name": "AliyunOSSReadOnlyAccess"}]}, "没有策略"),
            ({"policies": [OSS_READ, ECS_READ, CUSTOM]}, "最多"),
            ({"policies": []}, "至少"),
            ({"cloud_user": "someone-else"}, "自己"),
            ({"days": 0}, "天数"),
            ({"days": True}, "天数"),
            ({"account": "1000000000000009"}, "自己"),
        ]
        for override, fragment in cases:
            payload = {**env.payload(), **override}
            with self.assertRaises(FlowError, msg=override) as ctx:
                env.flows.submit(
                    applicant=LI,
                    email="li.si@wuji.tech",
                    template_id=POLICY_TEMPLATE,
                    payload=payload,
                    reason="项目需要读取训练数据",
                )
            self.assertIn(fragment, str(ctx.exception))

    def test_directory_missing_blocks_submit(self):
        env = Env()
        env.snapshot = None
        with self.assertRaises(FlowError):
            env.submit()

    def test_overlapping_open_request_refused(self):
        env = Env()
        first = env.submit(OSS_READ)
        with self.assertRaises(FlowError) as ctx:
            env.submit(ECS_READ, OSS_READ)
        self.assertIn(first["id"], str(ctx.exception))
        env.submit(ECS_READ)  # 不重叠的可以


# ── 开通、复核、回收 ──────────────────────────────────────────────────────


class ExecuteRevokeTests(unittest.TestCase):
    def test_grant_attaches_and_records_baseline(self):
        env = Env()
        done = env.grant(OSS_READ, ECS_READ)
        self.assertEqual(done["status"], t.DONE, done)
        self.assertEqual(done["preexisting_policies"], [])
        self.assertIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)
        self.assertTrue(done["expires_at_ts"])

    def test_rules_tightened_during_approval_blocks(self):
        env = Env()
        ticket = env.submit(OSS_READ)
        env.rules = pol.parse_rules({"deny": ["AliyunOSS*"]})
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        done = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.FAILED)
        self.assertEqual(env.executor.attached, set())

    def test_policy_removed_or_days_lowered_blocks(self):
        for change in ("removed", "days"):
            env = Env()
            ticket = env.submit(OSS_READ, days=100)
            if change == "removed":
                env.snapshot["accounts"][0]["policies"] = [
                    p
                    for p in env.snapshot["accounts"][0]["policies"]
                    if p["name"] != OSS_READ["name"]
                ]
            else:
                env.rules = pol.parse_rules({"max_days": {"low": 60}})
            env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
            self.assertEqual(env.flows.sync(ticket["id"], force=True)["status"], t.FAILED, change)
            self.assertEqual(env.executor.attached, set())

    def test_loosened_rules_still_execute(self):
        env = Env()
        ticket = env.submit(PAI_FULL, days=30)
        env.rules = pol.parse_rules({"max_days": {"high": 90}})
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        self.assertEqual(env.flows.sync(ticket["id"], force=True)["status"], t.DONE)

    def test_unapproved_never_attaches(self):
        env = Env()
        ticket = env.submit(OSS_READ)
        with self.assertRaises(FlowError):
            env.flows.execute(ticket["id"], actor="admin")
        self.assertEqual(env.executor.actions, [])

    def test_expiry_detaches_but_keeps_preexisting(self):
        env = Env()
        env.executor.attached.add(("lisi", "System", "AliyunECSReadOnlyAccess"))
        done = env.grant(OSS_READ, ECS_READ, days=7)
        self.assertEqual(done["preexisting_policies"], ["System:AliyunECSReadOnlyAccess"])
        env.now[0] += 8 * 86400
        env.flows.revoke_expired()
        self.assertEqual(env.store.get(done["id"])["status"], t.REVOKED)
        self.assertNotIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)
        self.assertIn(("lisi", "System", "AliyunECSReadOnlyAccess"), env.executor.attached)

    def test_renewal_before_expiry_does_not_become_permanent(self):
        env = Env()
        a = env.grant(OSS_READ, days=7)
        env.now[0] += 86400
        b = env.grant(OSS_READ, days=30)
        self.assertEqual(b["preexisting_policies"], [])  # 是 A 授予的，不算原有
        env.now[0] += 7 * 86400
        env.flows.revoke_expired()
        self.assertEqual(env.store.get(a["id"])["status"], t.REVOKED)
        self.assertIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)
        env.now[0] += 30 * 86400
        env.flows.revoke_expired()
        self.assertNotIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)

    def test_partial_failure_retry_and_close_do_not_leave_permanent_grant(self):
        env = Env()
        env.executor.fail_attach = "AliyunOSSReadOnlyAccess"
        first = env.grant(ECS_READ, OSS_READ, days=7)
        self.assertEqual(first["status"], t.FAILED)
        self.assertIn(("lisi", "System", "AliyunECSReadOnlyAccess"), env.executor.attached)
        env.flows.close(first["id"], actor="admin", note="不要了")
        env.executor.fail_attach = None
        second = env.grant(ECS_READ, days=7)
        self.assertEqual(second["preexisting_policies"], [])
        env.now[0] += 8 * 86400
        env.flows.revoke_expired()
        self.assertNotIn(("lisi", "System", "AliyunECSReadOnlyAccess"), env.executor.attached)

    def test_interrupted_renewal_without_baseline_does_not_keep_grant(self):
        env = Env()
        a = env.grant(OSS_READ, days=7)
        env.now[0] += 86400
        b = env.submit(OSS_READ, days=30)
        env.feishu.instances[b["approval"]["instance_code"]]["status"] = "APPROVED"
        env.store.update(
            b["id"], actor="feishu", expect=[t.PENDING], to=t.APPROVED, event="approval_approved"
        )
        # 开通到一半进程退出：还没记下基线
        env.store.update(
            b["id"], actor="system", expect=[t.APPROVED], to=t.EXECUTING, event="execute_start"
        )
        env.now[0] += 7 * 86400
        env.flows.revoke_expired()
        self.assertEqual(env.store.get(a["id"])["status"], t.REVOKED)
        self.assertNotIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)
        env.flows.recover_stuck(actor="system")
        done = env.flows.execute(b["id"], actor="admin")
        self.assertEqual(done["preexisting_policies"], [])
        env.now[0] += 31 * 86400
        env.flows.revoke_expired()
        self.assertNotIn(("lisi", "System", "AliyunOSSReadOnlyAccess"), env.executor.attached)

    def test_reassigned_sub_account_blocks_execution(self):
        env = Env()
        ticket = env.submit(OSS_READ)
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.flows._roster = lambda: people_mod.parse({"schema": people_mod.SCHEMA, "people": []})
        done = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.FAILED)
        self.assertEqual(env.executor.attached, set())

    def test_retry_keeps_first_baseline(self):
        env = Env()
        env.executor.fail_attach = "AliyunOSSReadOnlyAccess"
        failed = env.grant(ECS_READ, OSS_READ, days=7)
        env.executor.fail_attach = None
        done = env.flows.execute(failed["id"], actor="admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["preexisting_policies"], [])


# ── 权限列表 ─────────────────────────────────────────────────────────────


class PolicyOptionsTests(unittest.TestCase):
    def rows(self, env, union_id="on_li"):
        data = env.flows.policy_options(union_id)
        return data, {p["name"]: p for p in data["accounts"][0]["policies"]} if data[
            "accounts"
        ] else {}

    def test_states(self):
        env = Env(
            current={
                "aliyunecsreadonlyaccess": ("AliyunECSReadOnlyAccess", "经用户组 wuji_dev"),
                "aliyunpaifullaccess": ("AliyunPAIFullAccess", "直接授予"),
            }
        )
        ticket = env.submit(OSS_READ)
        data, rows = self.rows(env)
        acc = data["accounts"][0]
        self.assertEqual(
            (acc["platform"], acc["account"], acc["cloud_user"]), ("aliyun", ACC, "lisi")
        )
        self.assertEqual(acc["total"], 5)
        self.assertEqual(data["max_per_request"], 10)
        self.assertEqual(rows["AliyunOSSReadOnlyAccess"]["state"], "pending")
        self.assertEqual(rows["AliyunOSSReadOnlyAccess"]["request_id"], ticket["id"])
        self.assertEqual(rows["AliyunECSReadOnlyAccess"]["state"], "owned")
        self.assertEqual(rows["AliyunECSReadOnlyAccess"]["state_note"], "经用户组 wuji_dev")
        self.assertEqual(rows["AliyunPAIFullAccess"]["state_note"], "直接授予")
        self.assertEqual(rows["AdministratorAccess"]["state"], "unavailable")
        self.assertEqual(rows["team-data-reader"]["risk"], "high")
        # 自定义策略默认不开放
        self.assertEqual(rows["team-data-reader"]["state"], "unavailable")
        self.assertEqual(rows["team-data-reader"]["state_note"], pol.CUSTOM_NOTE)
        self.assertEqual(rows["team-data-reader"]["max_days"], 30)

    def test_owned_shows_expiry_of_active_grant(self):
        env = Env()
        done = env.grant(OSS_READ, days=30)
        env.current = {"aliyunossreadonlyaccess": ("AliyunOSSReadOnlyAccess", "直接授予")}
        _, rows = self.rows(env)
        self.assertEqual(rows["AliyunOSSReadOnlyAccess"]["expires_at"], done["expires_at"])

    def test_fresh_grant_is_owned_before_snapshot_refresh(self):
        env = Env(current={})
        done = env.grant(OSS_READ, days=30)
        _, rows = self.rows(env)
        row = rows["AliyunOSSReadOnlyAccess"]
        self.assertEqual(row["state"], "owned")
        self.assertEqual(row["request_id"], done["id"])
        self.assertEqual(row["expires_at"], done["expires_at"])
        env.now[0] += 31 * 86400  # 到期后不再算已拥有
        _, rows = self.rows(env)
        self.assertEqual(rows["AliyunOSSReadOnlyAccess"]["state"], "available")

    def test_unknown_snapshot_is_available(self):
        env = Env(current=None)
        _, rows = self.rows(env)
        self.assertEqual(rows["AliyunECSReadOnlyAccess"]["state"], "available")

    def test_other_users_tickets_invisible_and_no_account_no_rows(self):
        env = Env()
        env.submit(OSS_READ)
        data = env.flows.policy_options("on_new")
        self.assertEqual(data["accounts"], [])
        self.assertEqual(env.flows.policy_options("")["accounts"], [])

    def test_not_collected_is_error(self):
        env = Env()
        env.snapshot = None
        data, _ = self.rows(env)
        self.assertEqual(data["accounts"][0]["policies"], [])
        self.assertEqual(data["accounts"][0]["error"], "权限列表还没采集")

    def test_http_route_adds_labels_and_forbids_empty_union_id(self):
        env = Env()
        api = RequestsApi(lambda: env.flows, account_label=lambda p, a: "阿里云主账号")
        caller = Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "", admin=True)
        status, data = api.handle("GET", "/api/policies", {}, None, caller)
        self.assertEqual(status, 200)
        self.assertEqual(data["accounts"][0]["account_label"], "阿里云主账号")
        status, _ = api.handle("POST", "/api/policies", {}, {}, caller)
        self.assertEqual(status, 405)
        empty = Caller("", "x", "", "", "", admin=False)
        self.assertEqual(api.handle("GET", "/api/policies", {}, None, empty)[0], 403)

    def test_admin_rules_overview_only_for_admins(self):
        env = Env()
        api = RequestsApi(lambda: env.flows, account_label=lambda p, a: "阿里云主账号")
        admin = Caller("on_admin", "管理员", "", "ou_a", "", admin=True)
        status, data = api.handle("GET", "/api/admin/policies", {}, None, admin)
        self.assertEqual(status, 200)
        acc = data["accounts"][0]
        self.assertEqual(acc["account_label"], "阿里云主账号")
        rows = {p["name"]: p for p in acc["policies"]}
        self.assertFalse(rows["AdministratorAccess"]["open"])
        self.assertTrue(rows["AdministratorAccess"]["reason"])
        self.assertFalse(rows["team-data-reader"]["open"])  # 自定义策略默认不开放
        self.assertTrue(rows["AliyunOSSReadOnlyAccess"]["open"])
        self.assertEqual(rows["AliyunOSSReadOnlyAccess"]["max_days"], 180)
        self.assertIn("AliyunRAM*", data["rules"]["deny_families"])
        self.assertFalse(data["rules"]["allow_custom"])
        # 不开放的排在前面，方便对照
        self.assertFalse(acc["policies"][0]["open"])
        employee = Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "", admin=False)
        self.assertEqual(api.handle("GET", "/api/admin/policies", {}, None, employee)[0], 403)
        self.assertEqual(api.handle("POST", "/api/admin/policies", {}, {}, admin)[0], 405)
        # 不能借前缀绕进申请单接口
        ticket = env.submit(OSS_READ)
        for path in (
            f"/api/admin/policies/{ticket['id']}",
            f"/api/admin/policies/{ticket['id']}/close",
        ):
            self.assertEqual(api.handle("POST", path, {}, {}, admin)[0], 404)
        self.assertEqual(
            api.handle("GET", f"/api/admin/other/{ticket['id']}", {}, None, admin)[0], 404
        )
        self.assertEqual(
            api.handle("GET", f"/api/admin/requests/{ticket['id']}", {}, None, admin)[0], 200
        )

    def test_backend_current_policies_from_snapshot(self):
        from delivery.server import Backend

        d = Path(tempfile.mkdtemp())
        inventory = {
            "captured_at": "2026-09-15T10:00:00+08:00",
            "accounts": [
                {
                    "platform": "aliyun",
                    "account": ACC,
                    "users": [
                        {
                            "name": "lisi",
                            "policies": ["AliyunPAIFullAccess", "AliyunOSSFullAccess @资源组:rg-1"],
                            "groups": ["dev"],
                        }
                    ],
                    "groups": [
                        {
                            "name": "dev",
                            "policies": ["AliyunECSReadOnlyAccess"],
                            "members": ["lisi"],
                        }
                    ],
                },
                {"platform": "volcano", "account": "2000000001", "error": "denied"},
            ],
        }
        (d / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        backend = Backend(inventory_path=str(d / "inventory.json"))
        got = backend.current_policies("aliyun", ACC, "lisi")
        self.assertEqual(got["aliyunpaifullaccess"], ("AliyunPAIFullAccess", "直接授予"))
        self.assertEqual(got["aliyunecsreadonlyaccess"][1], "经用户组 dev")
        self.assertNotIn("aliyunossfullaccess", got)
        self.assertIsNone(backend.current_policies("volcano", "2000000001", "lisi"))
        self.assertIsNone(backend.current_policies("aliyun", ACC, "nobody"))


# ── 审批跳转链接 ───────────────────────────────────────────────────────────


class ApprovalLinkTests(unittest.TestCase):
    def test_default_links(self):
        links = instance_links(None, "81D31358-93AF-92D6-7425-01A5D67C4E71")
        self.assertTrue(links["pc"].startswith("https://applink.feishu.cn/"))
        self.assertIn("instanceId%3D81D31358-93AF-92D6-7425-01A5D67C4E71", links["pc"])
        self.assertIn("pages%2Fdetail%2Findex", links["mobile"])
        self.assertIn("{instance_code}", DEFAULT_INSTANCE_URL)

    def test_bad_code_gives_no_link(self):
        for code in ("", None, "a/b", "x&mode=evil", "a" * 200):
            self.assertEqual(instance_links(None, code), {"pc": "", "mobile": ""}, code)

    def test_config_override_and_validation(self):
        d = Path(tempfile.mkdtemp())
        base = {"approval_code": "A", "widgets": dict(CONFIG.widgets)}
        path = d / "approval.json"
        path.write_text(
            json.dumps({**base, "instance_url": "https://lark.example/i/{instance_code}"})
        )
        config = ApprovalConfig.load(str(path))
        self.assertEqual(instance_links(config, "C-1")["pc"], "https://lark.example/i/C-1")
        self.assertTrue(
            instance_links(config, "C-1")["mobile"].startswith("https://applink.feishu.cn/")
        )
        for bad in (
            "http://x/{instance_code}",
            "https://x/no-placeholder",
            "javascript:{instance_code}",
        ):
            path.write_text(json.dumps({**base, "instance_url_mobile": bad}))
            with self.assertRaises(ApprovalError, msg=bad):
                ApprovalConfig.load(str(path))

    def test_ticket_view_exposes_links_and_policies_only_to_owner_and_admin(self):
        env = Env()
        ticket = env.submit(OSS_READ)
        api = RequestsApi(lambda: env.flows)
        li = Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "", admin=False)
        status, data = api.handle("GET", f"/api/requests/{ticket['id']}", {}, None, li)
        self.assertEqual(status, 200)
        view = data["request"]
        self.assertTrue(view["approval_url"].startswith("https://applink.feishu.cn/"))
        self.assertTrue(view["approval_url_mobile"])
        self.assertEqual(
            view["template"]["policies"],
            [{"type": "System", "name": "AliyunOSSReadOnlyAccess", "risk": "low"}],
        )
        admin = Caller("on_admin", "管理员", "", "ou_a", "", admin=True)
        status, data = api.handle("GET", f"/api/admin/requests/{ticket['id']}", {}, None, admin)
        self.assertTrue(data["request"]["approval_url"])
        other = Caller("on_new", "新人", "", "ou_new", "", admin=False)
        self.assertEqual(
            api.handle("GET", f"/api/requests/{ticket['id']}", {}, None, other)[0], 404
        )


# ── 执行器 ───────────────────────────────────────────────────────────────


class ExecutorPolicyTests(unittest.TestCase):
    def aliyun(self, responses):
        import urllib.parse

        calls = []

        def send(url):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            return responses.get(query["Action"], (200, {}))

        base = {"GetCallerIdentity": (200, {"AccountId": ACC}), "GetUser": (200, {"User": {}})}
        return AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send), calls, base

    def test_aliyun_detach_deleted_custom_policy_is_done(self):
        _, _, base = self.aliyun({})
        gone = (404, {"Code": "EntityNotExist.Policy", "Message": "x"})
        ex, _, _ = self.aliyun({**base, "DetachPolicyFromUser": gone})
        ex.detach_policy("lisi", "Custom", "team-data-reader")
        with self.assertRaises(aliyun.AliyunError):
            ex.detach_policy("lisi", "System", "AliyunNoSuchAccess")

    def test_aliyun_attach_detach_has(self):
        ex, calls, base = self.aliyun({})
        ex, calls, _ = self.aliyun(
            {
                **base,
                "AttachPolicyToUser": (
                    409,
                    {"Code": "EntityAlreadyExists.User.Policy", "Message": "x"},
                ),
                "DetachPolicyFromUser": (
                    404,
                    {"Code": "EntityNotExist.User.Policy", "Message": "x"},
                ),
                "ListPoliciesForUser": (
                    200,
                    {
                        "Policies": {
                            "Policy": [
                                {"PolicyName": "AliyunOSSReadOnlyAccess", "PolicyType": "System"}
                            ]
                        }
                    },
                ),
            }
        )
        ex.attach_policy("lisi", "System", "AliyunOSSReadOnlyAccess")
        ex.detach_policy("lisi", "System", "AliyunOSSReadOnlyAccess")
        self.assertTrue(ex.has_policy("lisi", "System", "AliyunOSSReadOnlyAccess"))
        self.assertFalse(ex.has_policy("lisi", "Custom", "AliyunOSSReadOnlyAccess"))
        attach = next(c for c in calls if c["Action"] == "AttachPolicyToUser")
        self.assertEqual(
            {k: attach[k] for k in ("UserName", "PolicyType", "PolicyName")},
            {"UserName": "lisi", "PolicyType": "System", "PolicyName": "AliyunOSSReadOnlyAccess"},
        )
        self.assertNotIn("ResourceGroupId", attach)

    def test_aliyun_errors_surface(self):
        _, _, base = self.aliyun({})
        ex, _, _ = self.aliyun(
            {
                **base,
                "AttachPolicyToUser": (409, {"Code": "LimitExceeded.User.Policy", "Message": "x"}),
            }
        )
        with self.assertRaises(ProvisionError) as ctx:
            ex.attach_policy("lisi", "System", "AliyunOSSReadOnlyAccess")
        self.assertIn("上限", str(ctx.exception))
        ex, _, _ = self.aliyun(
            {
                **base,
                "DetachPolicyFromUser": (404, {"Code": "EntityNotExist.Policy", "Message": "x"}),
            }
        )
        with self.assertRaises(aliyun.AliyunError):
            ex.detach_policy("lisi", "System", "AliyunTypoAccess")

    def volcano(self, handler):
        import urllib.parse

        from delivery.clouds import volcano
        from delivery.provision import VolcanoExecutor

        calls = []

        def send(url, headers, data=None):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            if query["Action"] == "ListUsers":
                return 200, {"Result": {"UserMetadata": [{"AccountId": "2000000001"}]}}
            if query["Action"] == "GetUser":
                return 200, {"Result": {"User": {}}}
            return handler(query)

        return VolcanoExecutor("2000000001", volcano.Credentials("AK", "SK"), transport=send), calls

    def test_volcano_policy_calls(self):
        def err(code):
            return 409, {"ResponseMetadata": {"Error": {"Code": code, "Message": "x"}}}

        attached = {
            "AttachedPolicyMetadata": [
                {"PolicyName": "TOSReadOnlyAccess", "PolicyType": "System"},
                {
                    "PolicyName": "VPCFullAccess",
                    "PolicyType": "System",
                    "PolicyScope": [{"PolicyScopeType": "Project", "ProjectName": "p1"}],
                },
            ]
        }

        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {"Result": attached}
            if q["Action"] == "AttachUserPolicy":
                return err("PolicyAttachConflict")
            if q["Action"] == "DetachUserPolicy":
                return err("PolicyDetachConflict")
            return 400, {}

        ex, calls = self.volcano(handler)
        self.assertTrue(ex.has_policy("lisi", "System", "TOSReadOnlyAccess"))
        self.assertFalse(ex.has_policy("lisi", "System", "VPCFullAccess"))  # 只在项目里生效
        ex.attach_policy("lisi", "System", "TOSReadOnlyAccess")
        ex.detach_policy("lisi", "System", "TOSReadOnlyAccess")
        ex.detach_policy("lisi", "System", "ECSFullAccess")  # 没授予过：不调撤销
        detaches = [c for c in calls if c["Action"] == "DetachUserPolicy"]
        self.assertEqual([c["PolicyName"] for c in detaches], ["TOSReadOnlyAccess"])

    def test_volcano_attach_other_error_raises(self):
        from delivery.clouds import volcano

        def handler(q):
            return 404, {"ResponseMetadata": {"Error": {"Code": "PolicyNotExist", "Message": "x"}}}

        ex, _ = self.volcano(handler)
        with self.assertRaises(volcano.VolcanoError):
            ex.attach_policy("lisi", "System", "TypoAccess")


# ── CLI ──────────────────────────────────────────────────────────────────


class CliGrantTests(unittest.TestCase):
    LISTING = {
        "captured_at": "",
        "max_per_request": 10,
        "accounts": [
            {
                "platform": "aliyun",
                "account": ACC,
                "account_label": "阿里云主账号",
                "cloud_user": "lisi",
                "error": "",
                "total": 2,
                "policies": [
                    {
                        "type": "System",
                        "name": "AliyunOSSReadOnlyAccess",
                        "description": "",
                        "service": "OSS",
                        "risk": "low",
                        "max_days": 180,
                        "state": "available",
                        "state_note": "",
                        "request_id": "",
                        "expires_at": "",
                    },
                    {
                        "type": "Custom",
                        "name": "dup",
                        "description": "",
                        "service": "",
                        "risk": "high",
                        "max_days": 30,
                        "state": "available",
                        "state_note": "",
                        "request_id": "",
                        "expires_at": "",
                    },
                    {
                        "type": "System",
                        "name": "dup",
                        "description": "",
                        "service": "",
                        "risk": "medium",
                        "max_days": 90,
                        "state": "available",
                        "state_note": "",
                        "request_id": "",
                        "expires_at": "",
                    },
                ],
            }
        ],
    }

    def run_grant(self, **kw):
        from delivery import cli_requests

        sent = []

        class Client:
            def request(self, method, path, body=None):
                sent.append((method, path, body))
                if method == "GET":
                    return CliGrantTests.LISTING
                return {
                    "request": {
                        "id": "REQ-1",
                        "status": "pending_approval",
                        "status_label": "待审批",
                        "summary": "s",
                        "approval_url": "",
                    }
                }

        args = argparse.Namespace(
            account=f"aliyun/{ACC}",
            policy=["aliyunossreadonlyaccess"],
            type=None,
            days=30,
            reason="需要读数据",
        )
        for k, v in kw.items():
            setattr(args, k, v)
        code = cli_requests._grant(Client(), args)
        return code, sent

    def test_grant_payload(self):
        code, sent = self.run_grant()
        self.assertEqual(code, 0)
        method, path, body = sent[-1]
        self.assertEqual((method, path), ("POST", "/api/requests"))
        self.assertEqual(body["template_id"], "policy")
        self.assertEqual(
            body["payload"],
            {
                "platform": "aliyun",
                "account": ACC,
                "cloud_user": "lisi",
                "days": 30,
                "policies": [{"type": "System", "name": "AliyunOSSReadOnlyAccess"}],
            },
        )

    def test_ambiguous_name_needs_type(self):
        from delivery.cli_requests import ClientError

        with self.assertRaises(ClientError):
            self.run_grant(policy=["dup"])
        _, sent = self.run_grant(policy=["dup"], type="Custom")
        self.assertEqual(sent[-1][2]["payload"]["policies"], [{"type": "Custom", "name": "dup"}])

    def test_unknown_account_or_policy(self):
        from delivery.cli_requests import ClientError

        with self.assertRaises(ClientError):
            self.run_grant(account="volcano/2000000001")
        with self.assertRaises(ClientError):
            self.run_grant(policy=["AliyunNoSuch"])


if __name__ == "__main__":
    unittest.main()

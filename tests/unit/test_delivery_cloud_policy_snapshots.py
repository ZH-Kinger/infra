"""云上那四份策略副本和代码里的前缀表**对不对得上**（`deploy/panel/cloud-policies/`）。

为什么这一条必须有测试
──────────────────────
前缀是分两边生效的：代码这边决定「面板打算建一个叫什么的号」，云那边的策略
决定「这把 AK 到底动得了哪些号」。两边各写各的字面量，而**改错的一边不会报错**：

  · issuer 的 Allow 漏改 → 凭证发不出来（火山更惨，它的每一张凭证都走长期路径，
    没有 STS 兜底，漏改就是 100% 发不出来）；
  · executor 的 Deny 漏改 → **保护没了**，离职流程能去动一个程序发的凭证号。

2026-09-23 把内部凭证从 `tempak-` 改成 `staff-` 时，这两处都漏了，是审计才抓出来的。
所以这里的样本名**全部从 `grants` 里的常量拼出来**：下一次改名，只动一边就过不了这一关。

副本是从云上导出的，**以云上为准**。这里失败的意思是「云上和代码不一致」，
修法是去云上改策略再重新导出，不是改这个文件里的期望值。

纯读文件，不联网。
"""

from __future__ import annotations

import fnmatch
import json
import unittest
from pathlib import Path

from delivery import grants, offboard

POLICY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "panel" / "cloud-policies"

#: (云, 动作前缀, 发放身份副本, 开通身份副本, 挂策略的动作名, 停号会调的动作)
#: 两朵云的动作名只差一个前缀和几个词，其余判据完全一样 —— 一份用例管两朵
CLOUDS = (
    (
        "aliyun",
        "ram",
        "aliyun.wuji-panel-issuer",
        "aliyun.wuji-panel-executor",
        "AttachPolicyToUser",
        ("DeleteLoginProfile", "UpdateAccessKey"),
    ),
    (
        "volcano",
        "iam",
        "volcano.wuji-panel-issuer",
        "volcano.wuji-panel-executor",
        "AttachUserPolicy",
        ("DeleteLoginProfile", "UpdateAccessKey"),
    ),
)

#: 一个该被覆盖到的具体名字，按两族各造一个（**从常量拼**，改名这里自动跟着变）
INTERNAL_USER = grants.USER_PREFIX + "alice-9a1b2c"
EXTERNAL_USER = grants.EXTERNAL_USER_PREFIX + "yuanke-9a1b2c"
INTERNAL_POLICY = grants.policy_name(INTERNAL_USER)
EXTERNAL_POLICY = grants.policy_name(EXTERNAL_USER)


def load(name: str) -> dict:
    return json.loads((POLICY_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _listed(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _tail(arn: str) -> str:
    """`acs:ram:*:123:user/staff-*` / `trn:iam::123:policy/x-*` → `user/staff-*`。

    两朵云的 ARN 前半段写法不同（`acs:` vs `trn:`），但「类型/名字」这一段一样 ——
    只比这一段，一份用例同时管两朵云。
    """
    return str(arn or "").rsplit(":", 1)[-1]


def covers(doc: dict, *, effect: str, action: str, resource: str) -> bool:
    """这份策略里有没有一条 `effect` 语句，既含 `action` 又盖住 `resource`。

    `action` / `resource` 传**具体值**，策略里的那一侧才是允许带 `*` 的模式 ——
    反过来写的话 `ram:*` 这种语句会被漏掉，而云上真是按它判的。
    """
    for stmt in doc.get("Statement") or ():
        if stmt.get("Effect") != effect:
            continue
        acts = [str(a).lower() for a in _listed(stmt.get("Action"))]
        if not any(fnmatch.fnmatch(action.lower(), a) for a in acts):
            continue
        if any(fnmatch.fnmatch(resource, _tail(a)) for a in _listed(stmt.get("Resource"))):
            return True
    return False


class SnapshotFileTests(unittest.TestCase):
    def test_all_four_snapshots_exist_and_parse(self):
        """少一份不是「少测一朵云」，是**那朵云根本没人看过策略长什么样** ——
        发放身份的策略只在云控制台里，没有副本就没有任何评审留痕。"""
        for _, _, issuer, executor, _, _ in CLOUDS:
            for name in (issuer, executor):
                self.assertTrue(load(name).get("Statement"), name)

    def test_aliyun_keeps_version_and_volcano_does_not(self):
        # 方言差异，和 grants.build_policy 里那条一致：火山带 Version 会被拒
        self.assertIn("Version", load("aliyun.wuji-panel-issuer"))
        self.assertNotIn("Version", load("volcano.wuji-panel-issuer"))


class IssuerAllowTests(unittest.TestCase):
    """发放身份：两族的子账号和策略都要发得出、也撤得掉。"""

    USER_ACTIONS = ("CreateUser", "GetUser", "DeleteUser")
    POLICY_ACTIONS = ("CreatePolicy", "GetPolicy", "DeletePolicy")

    def test_both_user_families_can_be_created_and_deleted(self):
        for cloud, ns, issuer, _, _, _ in CLOUDS:
            doc = load(issuer)
            for user in (INTERNAL_USER, EXTERNAL_USER):
                for act in self.USER_ACTIONS:
                    with self.subTest(cloud=cloud, user=user, action=act):
                        self.assertTrue(
                            covers(
                                doc,
                                effect="Allow",
                                action=f"{ns}:{act}",
                                resource=f"user/{user}",
                            )
                        )

    def test_both_policy_families_can_be_created_and_deleted(self):
        """**撤销比发放更要紧**：发不出来当场就知道了，删不掉是等到出事那天才发现。"""
        for cloud, ns, issuer, _, _, _ in CLOUDS:
            doc = load(issuer)
            for policy in (INTERNAL_POLICY, EXTERNAL_POLICY):
                for act in self.POLICY_ACTIONS:
                    with self.subTest(cloud=cloud, policy=policy, action=act):
                        self.assertTrue(
                            covers(
                                doc,
                                effect="Allow",
                                action=f"{ns}:{act}",
                                resource=f"policy/{policy}",
                            )
                        )

    def test_the_policy_can_actually_be_attached_to_the_user(self):
        """挂载那条语句要**同时**盖住 user 和 policy 两侧。这是最容易漏的一处：
        建号能建、造策略能造，挂的时候 403 —— 而那时号已经建出来了。"""
        for cloud, ns, issuer, _, attach, _ in CLOUDS:
            doc = load(issuer)
            for kind, name in (
                ("user", INTERNAL_USER),
                ("policy", INTERNAL_POLICY),
                ("user", EXTERNAL_USER),
                ("policy", EXTERNAL_POLICY),
            ):
                with self.subTest(cloud=cloud, resource=f"{kind}/{name}"):
                    self.assertTrue(
                        covers(
                            doc,
                            effect="Allow",
                            action=f"{ns}:{attach}",
                            resource=f"{kind}/{name}",
                        )
                    )

    def test_the_issuer_cannot_touch_a_real_person(self):
        """发放身份只该动它自己发出来的号。盖到真人身上的话，面板逻辑出个漏洞
        就能把一个在职同事的号删掉 —— 而这正是把发放和开通拆成两把 AK 的理由。"""
        for cloud, ns, issuer, _, _, _ in CLOUDS:
            doc = load(issuer)
            for name in ("lisi", "xuzhiyuan", "finance"):
                with self.subTest(cloud=cloud, user=name):
                    self.assertFalse(
                        covers(
                            doc, effect="Allow", action=f"{ns}:DeleteUser", resource=f"user/{name}"
                        )
                    )


class ExecutorDenyTests(unittest.TestCase):
    """开通身份：程序发的号在云上也有一道 Deny，不只靠代码里的名单。"""

    #: 两朵云各挑一个「一定被 Deny 名单盖住」的动作作为探针
    PROBE = {"aliyun": "CreateUser", "volcano": "DeleteLoginProfile"}

    def test_both_prefixes_are_denied_on_both_clouds(self):
        """这一条就是审计抓出来的那处：改了前缀、忘了改云上的 Deny。"""
        for cloud, ns, _, executor, _, _ in CLOUDS:
            doc = load(executor)
            for user in (INTERNAL_USER, EXTERNAL_USER):
                with self.subTest(cloud=cloud, user=user):
                    self.assertTrue(
                        covers(
                            doc,
                            effect="Deny",
                            action=f"{ns}:{self.PROBE[cloud]}",
                            resource=f"user/{user}",
                        ),
                        f"{user} 没有云上的 Deny 兜底",
                    )

    def test_a_real_person_is_not_denied(self):
        """反向：Deny 写宽了，离职的人就停不掉了，而那比多停一个号危险得多。"""
        for cloud, ns, _, executor, _, _ in CLOUDS:
            doc = load(executor)
            for name in ("lisi", "xuzhiyuan"):
                with self.subTest(cloud=cloud, user=name):
                    self.assertFalse(
                        covers(
                            doc,
                            effect="Deny",
                            action=f"{ns}:{self.PROBE[cloud]}",
                            resource=f"user/{name}",
                        )
                    )

    #: 离职停号真正会调的动作（`src/delivery/provision.py:489` disable_user +
    #: `:540` delete_user）。**云上的 Deny 必须写在这一组上** —— 只写在建号那几个动作上
    #: 的话，看起来「有 Deny」，而停号那条路照样能动受保护的号
    DISABLE_ACTIONS = (
        "GetLoginProfile",
        "DeleteLoginProfile",
        "ListAccessKeys",
        "UpdateAccessKey",
        "DeleteAccessKey",
        "DeleteUser",
    )

    def test_the_deny_covers_the_actions_offboarding_actually_uses(self):
        """**动作集也要对齐，不只是名字。**

        代码这边 `offboard.protected()` 是按名字挡的，云上这道是第二层；
        第二层只有在「挡住停号真正会调的那几个动作」时才算数。
        """
        for cloud, ns, _, executor, _, _ in CLOUDS:
            doc = load(executor)
            for action in self.DISABLE_ACTIONS:
                for user in (INTERNAL_USER, EXTERNAL_USER):
                    with self.subTest(cloud=cloud, action=action, user=user):
                        self.assertTrue(
                            covers(
                                doc,
                                effect="Deny",
                                action=f"{ns}:{action}",
                                resource=f"user/{user}",
                            )
                        )

    def test_every_code_protected_prefix_has_a_cloud_deny(self):
        """回归（曾经两边名单没对齐）：`deploy/panel/cloud-policies/README.md` 写着
        「受保护的号在两份策略里都有 Deny，名单要和 `offboard.PROTECTED` 对齐」。

        实际差两项（代码挡、云上不挡）：

          · `temp-ak-`（历史写法，`src/delivery/grants.py:54` 在表里，
            `src/delivery/offboard.py:55` 的正则也单列了它）—— 两份 executor 副本写的都是
            `user/tempak*`（中间没有横杠），盖不住 `temp-ak-老号`；
          · `panel-` 在**阿里**那份里只列了三个具体名字
            （`panel-collector` / `panel-executor` / `panel-issuer`，第 8 条），
            不是 `panel-*`；火山那份是 `user/panel-*`，对的。

        差的是第二道闸，不是第一道：代码里 `offboard.protected()` 仍然挡着，
        所以今天不会出事。但 README 承诺的是「两层」，而这两项现在只有一层 ——
        哪天有人加一条按快照批量停号的路径忘了调 `protected()`，云上不会拦。

        期望：每个 `ISSUED_PREFIXES` 前缀在两份 executor 副本里都有 Deny。
        实际：`temp-ak-` 两朵云都缺，`panel-` 阿里缺。
        修法：把阿里那两条 Deny 的资源改成 `user/temp-ak-*` + `user/panel-*`
        （火山补 `user/temp-ak-*`），重新导出副本。
        """
        # subTest 同上：expectedFailure 里不能用
        for cloud, ns, _, executor, _, _ in CLOUDS:
            doc = load(executor)
            for prefix in grants.ISSUED_PREFIXES:
                name = f"{prefix}legacy-1"
                self.assertTrue(offboard.protected(name), f"{name} 代码这边就没挡")
                self.assertTrue(
                    covers(
                        doc,
                        effect="Deny",
                        action=f"{ns}:{self.PROBE[cloud]}",
                        resource=f"user/{name}",
                    ),
                    f"{cloud}: {name} 云上没有 Deny",
                )


if __name__ == "__main__":
    unittest.main()

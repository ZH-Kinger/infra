"""变更范围（scope）的硬规。

这一层决定「用户能自助改什么、管理员才能改什么」，写错的后果是把一个未经设计的
自助入口开给全员，所以规则全部在加载期强制，不做「警告后放行」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery.errors import PlatformSpecError
from delivery.registry import PlatformRegistry
from delivery.scopes import (
    SCOPE_FOUNDATION,
    SCOPE_WORKSPACE,
    ScopeCapabilities,
    default_scopes,
)

CAPS = {
    "auth": "oidc",
    "iac": "terraform",
    "plan": "native",
    "inventory": "api",
    "login": "sso",
    "apply": True,
    "policy_as_code": True,
    "require_approval": True,
    "status": "verified",
}


def scope(name, **over):
    data = {"apply": True, "require_approval": True}
    data.update(over)
    return ScopeCapabilities.from_mapping(
        name, data, platform="p", platform_apply=over.pop("_platform_apply", True)
    )


class ScopeRuleTests(unittest.TestCase):
    def test_foundation_apply_always_requires_approval(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            scope(SCOPE_FOUNDATION, require_approval=False)
        self.assertIn("审批", str(ctx.exception))

    def test_workspace_may_skip_approval(self):
        s = scope(SCOPE_WORKSPACE, require_approval=False)
        self.assertFalse(s.require_approval)

    def test_scope_cannot_exceed_platform_apply(self):
        # 平台整体还没真机验证（apply=false）时，不能被某个 scope 悄悄绕过去
        with self.assertRaises(PlatformSpecError) as ctx:
            ScopeCapabilities.from_mapping(
                SCOPE_WORKSPACE,
                {"apply": True, "require_approval": False},
                platform="p",
                platform_apply=False,
            )
        self.assertIn("上限", str(ctx.exception))

    def test_scope_may_be_stricter_than_platform(self):
        s = ScopeCapabilities.from_mapping(
            SCOPE_WORKSPACE,
            {"apply": False, "require_approval": False},
            platform="p",
            platform_apply=True,
        )
        self.assertFalse(s.apply)

    def test_unknown_scope_name_rejected(self):
        with self.assertRaises(PlatformSpecError):
            ScopeCapabilities.from_mapping(
                "sandbox",
                {"apply": False, "require_approval": True},
                platform="p",
                platform_apply=True,
            )

    def test_booleans_must_be_explicit(self):
        for bad in ("true", 1, None):
            with self.assertRaises(PlatformSpecError):
                ScopeCapabilities.from_mapping(
                    SCOPE_WORKSPACE,
                    {"apply": bad, "require_approval": True},
                    platform="p",
                    platform_apply=True,
                )

    def test_quota_gating_is_meaningless_for_foundation(self):
        with self.assertRaises(PlatformSpecError):
            ScopeCapabilities.from_mapping(
                SCOPE_FOUNDATION,
                {"apply": True, "require_approval": True, "quota_gated": True},
                platform="p",
                platform_apply=True,
            )

    def test_direct_construction_is_also_guarded(self):
        # frozen 挡不住重建；规则必须是类型不变量
        with self.assertRaises(PlatformSpecError):
            ScopeCapabilities(name=SCOPE_FOUNDATION, apply=True, require_approval=False)


class DefaultScopeTests(unittest.TestCase):
    def test_default_is_foundation_only_and_strict(self):
        # 没声明就默认最严：新平台接进来时作者可能还没想清楚哪些资源能自助，
        # 这时默认放开等于把未经设计的入口开给全员。
        scopes = default_scopes(platform_apply=True)
        self.assertEqual(set(scopes), {SCOPE_FOUNDATION})
        self.assertTrue(scopes[SCOPE_FOUNDATION].require_approval)

    def test_default_respects_platform_ceiling(self):
        scopes = default_scopes(platform_apply=False)
        self.assertFalse(scopes[SCOPE_FOUNDATION].apply)


class RegistryScopeTests(unittest.TestCase):
    def _load(self, payload):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "demo.json").write_text(json.dumps(payload), encoding="utf-8")
            return PlatformRegistry.load(d).get("demo")

    def test_scopes_default_when_absent(self):
        p = self._load({"id": "demo", "display": "D", "capabilities": CAPS})
        self.assertEqual(set(p.scopes), {SCOPE_FOUNDATION})

    def test_foundation_is_mandatory_when_scopes_declared(self):
        # 「这个平台上没有需要管理员把关的东西」对任何真实平台都不成立
        with self.assertRaises(PlatformSpecError) as ctx:
            self._load(
                {
                    "id": "demo",
                    "display": "D",
                    "capabilities": CAPS,
                    "scopes": {"workspace": {"apply": True, "require_approval": False}},
                }
            )
        self.assertIn("foundation", str(ctx.exception))

    def test_scopes_must_be_an_object(self):
        with self.assertRaises(PlatformSpecError):
            self._load(
                {"id": "demo", "display": "D", "capabilities": CAPS, "scopes": ["foundation"]}
            )

    def test_scopes_are_read_only(self):
        p = self._load({"id": "demo", "display": "D", "capabilities": CAPS})
        with self.assertRaises(TypeError):
            p.scopes["workspace"] = None


class ShippedScopeTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlatformRegistry.load()

    def test_every_platform_declares_foundation(self):
        for p in self.registry:
            self.assertIn(SCOPE_FOUNDATION, p.scopes, p.id)

    def test_foundation_always_requires_approval(self):
        for p in self.registry:
            self.assertTrue(p.scopes[SCOPE_FOUNDATION].require_approval, p.id)

    def test_no_scope_exceeds_its_platform_ceiling(self):
        for p in self.registry:
            for name, sc in p.scopes.items():
                if sc.apply:
                    self.assertTrue(p.capabilities.apply, f"{p.id}/{name}")

    def test_unverified_platforms_allow_no_scope_to_apply(self):
        # 火山 status=pending-verification，任何 scope 都不该能 apply
        for p in self.registry:
            if p.capabilities.status == "verified":
                continue
            for name, sc in p.scopes.items():
                self.assertFalse(sc.apply, f"{p.id}/{name}")


if __name__ == "__main__":
    unittest.main()

import unittest

from delivery.capabilities import Capabilities
from delivery.errors import PlatformSpecError

BASE = {
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


def spec(**over):
    data = dict(BASE)
    data.update(over)
    return data


class CapabilityParsingTests(unittest.TestCase):
    def test_parses_valid_spec(self):
        caps = Capabilities.from_mapping(BASE, platform="p")
        self.assertTrue(caps.keyless)
        self.assertFalse(caps.read_only)

    def test_rejects_unknown_enum_value(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            Capabilities.from_mapping(spec(auth="password"), platform="p")
        self.assertIn("auth", str(ctx.exception))

    def test_rejects_missing_field(self):
        data = dict(BASE)
        del data["plan"]
        with self.assertRaises(PlatformSpecError):
            Capabilities.from_mapping(data, platform="p")

    def test_booleans_must_be_explicit(self):
        # 字符串 "true" 在 Python 里为真；若直接 bool() 会被静默当成 True。
        for value in ("true", 1, None, "yes"):
            with self.assertRaises(PlatformSpecError):
                Capabilities.from_mapping(spec(apply=value), platform="p")

    def test_missing_boolean_is_rejected_not_defaulted(self):
        data = dict(BASE)
        del data["apply"]
        with self.assertRaises(PlatformSpecError):
            Capabilities.from_mapping(data, platform="p")


class CapabilityHardRuleTests(unittest.TestCase):
    def test_rule1_no_plan_means_no_apply(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            Capabilities.from_mapping(
                spec(iac="none", plan="none", inventory="ssh", apply=True), platform="p"
            )
        self.assertIn("蒙眼", str(ctx.exception))

    def test_rule1_no_plan_read_only_is_fine(self):
        caps = Capabilities.from_mapping(
            spec(iac="none", plan="none", inventory="ssh", apply=False), platform="p"
        )
        self.assertTrue(caps.read_only)

    def test_rule2_unreadable_platform_rejected(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            Capabilities.from_mapping(
                spec(iac="none", plan="none", inventory="none", apply=False), platform="p"
            )
        self.assertIn("漂移", str(ctx.exception))

    def test_rule3_terraform_requires_native_plan(self):
        with self.assertRaises(PlatformSpecError):
            Capabilities.from_mapping(spec(iac="terraform", plan="dryrun"), platform="p")

    def test_rule4_static_secret_apply_requires_approval(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            Capabilities.from_mapping(
                spec(
                    auth="static-secret",
                    iac="none",
                    plan="dryrun",
                    inventory="cli",
                    apply=True,
                    require_approval=False,
                ),
                platform="p",
            )
        self.assertIn("require_approval", str(ctx.exception))

    def test_rule4_oidc_may_skip_approval(self):
        caps = Capabilities.from_mapping(spec(require_approval=False), platform="p")
        self.assertTrue(caps.apply)

    def test_rule5_unverified_cannot_apply(self):
        with self.assertRaises(PlatformSpecError) as ctx:
            Capabilities.from_mapping(spec(status="pending-verification"), platform="p")
        self.assertIn("真机验证", str(ctx.exception))

    def test_rule5_unverified_read_only_is_fine(self):
        caps = Capabilities.from_mapping(
            spec(status="pending-verification", apply=False), platform="p"
        )
        self.assertFalse(caps.apply)


if __name__ == "__main__":
    unittest.main()

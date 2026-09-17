import json
import tempfile
import unittest
from pathlib import Path

from delivery.errors import PlatformNotFoundError, PlatformSpecError
from delivery.registry import PlatformRegistry

GOOD = {
    "id": "demo",
    "display": "演示平台",
    "short": "演示",
    "capabilities": {
        "auth": "oidc",
        "iac": "terraform",
        "plan": "native",
        "inventory": "api",
        "login": "sso",
        "apply": True,
        "policy_as_code": True,
        "require_approval": True,
        "status": "verified",
    },
}


class _TempDir:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        return self.tmp.name

    def __exit__(self, *exc):
        self.tmp.cleanup()


def write(directory, name, payload):
    with (Path(directory) / name).open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


class RegistryLoadTests(unittest.TestCase):
    def test_loads_and_looks_up(self):
        with _TempDir() as d:
            write(d, "demo.json", GOOD)
            reg = PlatformRegistry.load(d)
            self.assertEqual(len(reg), 1)
            self.assertIn("demo", reg)
            self.assertEqual(reg.get("demo").short, "演示")

    def test_short_falls_back_to_display(self):
        payload = dict(GOOD)
        payload.pop("short")
        with _TempDir() as d:
            write(d, "demo.json", payload)
            self.assertEqual(PlatformRegistry.load(d).get("demo").short, "演示平台")

    def test_unknown_platform_lists_known_ones(self):
        with _TempDir() as d:
            write(d, "demo.json", GOOD)
            reg = PlatformRegistry.load(d)
            with self.assertRaises(PlatformNotFoundError) as ctx:
                reg.get("nope")
            self.assertIn("demo", str(ctx.exception))

    def test_filename_must_match_id(self):
        # 重复 id 有检测，「张冠李戴」原先没有：aliyun.json 里写 id=volcano
        # 会被静默注册成 volcano，评审看文件名根本看不出来。
        with _TempDir() as d:
            write(d, "other.json", GOOD)
            with self.assertRaises(PlatformSpecError) as ctx:
                PlatformRegistry.load(d)
            self.assertIn("文件名", str(ctx.exception))

    def test_rejects_bad_id_characters(self):
        for bad in ("Demo", "de mo", "demo/../x", "demo:1"):
            payload = dict(GOOD, id=bad)
            with _TempDir() as d:
                write(d, "x.json", payload)
                with self.assertRaises(PlatformSpecError):
                    PlatformRegistry.load(d)

    def test_rejects_malformed_json(self):
        with _TempDir() as d:
            (Path(d) / "bad.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(PlatformSpecError) as ctx:
                PlatformRegistry.load(d)
            self.assertIn("JSON", str(ctx.exception))

    def test_empty_directory_is_an_error(self):
        with _TempDir() as d, self.assertRaises(PlatformSpecError):
            PlatformRegistry.load(d)

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(PlatformSpecError):
            PlatformRegistry.load("/nonexistent/platforms")

    def test_non_json_files_are_ignored(self):
        with _TempDir() as d:
            write(d, "demo.json", GOOD)
            (Path(d) / "README.md").write_text("not a descriptor", encoding="utf-8")
            self.assertEqual(len(PlatformRegistry.load(d)), 1)

    def test_accounts_and_notes_must_be_string_lists(self):
        for field, value in (("accounts", [1]), ("notes", "一条"), ("accounts", "x")):
            payload = dict(GOOD)
            payload[field] = value
            with _TempDir() as d:
                write(d, "x.json", payload)
                with self.assertRaises(PlatformSpecError):
                    PlatformRegistry.load(d)


class ShippedDescriptorTests(unittest.TestCase):
    """锁住仓库里真实的 platforms/*.json —— 新增平台若违反硬规，这里会红。"""

    def setUp(self):
        self.registry = PlatformRegistry.load()

    def test_all_shipped_descriptors_load(self):
        self.assertGreaterEqual(len(self.registry), 4)
        for platform in self.registry:
            self.assertTrue(platform.short, f"{platform.id} 缺 short")
            self.assertTrue(platform.display)

    def test_expected_platforms_present(self):
        for pid in ("aliyun", "volcano", "jiuzhang", "xiwang"):
            self.assertIn(pid, self.registry)

    def test_volcano_stays_read_only_until_verified(self):
        # 火山的 auth=oidc 是查文档得来的，尚未真机验证；验证通过前不许 apply。
        volcano = self.registry.get("volcano")
        self.assertEqual(volcano.capabilities.status, "pending-verification")
        self.assertFalse(volcano.capabilities.apply)

    def test_jiuzhang_is_bind_mode_and_read_only(self):
        # 九章接不了飞书 SSO（平台用自己的 Casdoor），只能绑定托管。
        jz = self.registry.get("jiuzhang")
        self.assertEqual(jz.capabilities.login, "bind")
        self.assertFalse(jz.capabilities.apply)
        self.assertEqual(jz.capabilities.plan, "dryrun")

    def test_jiuzhang_forbids_alab_create(self):
        # alab create 给空 payload 也会产生真实付费实例，必须留在禁用名单里。
        forbidden = self.registry.get("jiuzhang").adapter.get("forbidden") or []
        self.assertIn("alab create", forbidden)

    def test_jiuzhang_records_delete_ordering(self):
        # Training 引用 NAS，删除必须先任务后存储，否则 NAS 删不掉。
        depends = self.registry.get("jiuzhang").adapter.get("depends_on") or {}
        self.assertEqual(depends.get("TrainingInstance"), ["NASInstance"])

    def test_xiwang_is_manual_and_read_only(self):
        """曦望有控制台但没 CLI 也没有可调的接口：清单靠人维护，绝不做变更。

        inventory=manual 和 none 的区别不是程度，是谁负责 —— manual 在能力矩阵上
        看得见，none 是个黑洞。之前这条描述符写的是 ssh，那是错的。
        """
        xw = self.registry.get("xiwang")
        self.assertEqual(xw.capabilities.inventory, "manual")
        self.assertEqual(xw.capabilities.login, "password")
        self.assertEqual(xw.capabilities.plan, "none")
        self.assertFalse(xw.capabilities.apply)

    def test_manual_inventory_cannot_apply(self):
        from delivery.capabilities import Capabilities
        from delivery.errors import PlatformSpecError

        with self.assertRaises(PlatformSpecError):
            Capabilities.from_mapping(
                {
                    "auth": "static-secret",
                    "iac": "none",
                    "plan": "dryrun",
                    "inventory": "manual",
                    "login": "password",
                    "apply": True,
                    "policy_as_code": False,
                    "require_approval": True,
                    "status": "verified",
                },
                platform="probe",
            )

    # 注：规则④（非 OIDC + apply ⇒ 必须审批）不在这里断言。
    # 真出现违规描述符时 setUp 的 load() 会先抛 PlatformSpecError，这里只会得到
    # 一个 ERROR 而非有意义的 FAIL；而当前 appliable() 全是 keyless，循环体恒不执行。
    # 规则④的正经锁在 CapabilityHardRuleTests.test_rule4_*。


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from delivery.plan import (  # noqa: I001
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_REPLACE,
    ACTION_UPDATE,
    REDACTED,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    PlanParseError,
    from_terraform,
)


def tf(changes, **extra):
    doc = {"format_version": "1.2", "resource_changes": changes}
    doc.update(extra)
    return doc


def rc(address, actions, *, type_="alicloud_instance", before=None, after=None, **extra):
    item = {
        "address": address,
        "mode": "managed",
        "type": type_,
        "name": address.split(".")[-1],
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            # 打码标记默认给空 dict：缺键现在是硬错误（fail-closed），
            # 需要测缺键行为的用例自己用 change= 覆盖掉。
            "before_sensitive": {},
            "after_sensitive": {},
        },
    }
    item["change"].update(extra.pop("change", {}))
    item.update(extra)
    return item


class ActionNormalizationTests(unittest.TestCase):
    def test_create_update_delete(self):
        plan = from_terraform(
            tf(
                [
                    rc("a.x", ["create"]),
                    rc("a.y", ["update"]),
                    rc("a.z", ["delete"]),
                ]
            ),
            platform="aliyun",
            env="prod",
        )
        self.assertEqual(
            [c.action for c in plan.changes], [ACTION_CREATE, ACTION_UPDATE, ACTION_DELETE]
        )

    def test_replace_detected_in_both_orderings(self):
        # create_before_destroy 会把顺序倒过来，两种都必须识别成 replace。
        for actions in (["delete", "create"], ["create", "delete"]):
            plan = from_terraform(tf([rc("a.x", actions)]), platform="aliyun", env="prod")
            self.assertEqual(plan.changes[0].action, ACTION_REPLACE, actions)

    def test_noop_and_read_are_dropped(self):
        plan = from_terraform(
            tf([rc("a.x", ["no-op"]), rc("a.y", ["read"])]), platform="aliyun", env="prod"
        )
        self.assertTrue(plan.empty)

    def test_data_sources_are_dropped(self):
        # data source 读取不是变更，混进审批只会制造噪音。
        plan = from_terraform(
            tf([rc("data.a.x", ["read"], mode="data"), rc("a.y", ["create"])]),
            platform="aliyun",
            env="prod",
        )
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(plan.changes[0].address, "a.y")

    def test_unknown_action_raises(self):
        with self.assertRaises(PlanParseError):
            from_terraform(tf([rc("a.x", ["frobnicate"])]), platform="aliyun", env="prod")


class RedactionTests(unittest.TestCase):
    """计划会进飞书卡片和 CI Artifact，敏感值绝不能泄漏。"""

    def test_scalar_sensitive_value_is_redacted(self):
        item = rc(
            "a.x",
            ["create"],
            after={"name": "ok", "secret": "s3cr3t"},
            change={"after_sensitive": {"name": False, "secret": True}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        after = plan.changes[0].after
        self.assertEqual(after["name"], "ok")
        self.assertEqual(after["secret"], REDACTED)
        self.assertNotIn("s3cr3t", plan.to_json())

    def test_whole_subtree_sensitive(self):
        item = rc(
            "a.x",
            ["create"],
            after={"creds": {"ak": "AK", "sk": "SK"}},
            change={"after_sensitive": {"creds": True}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].after["creds"], REDACTED)
        self.assertNotIn("SK", plan.to_json())

    def test_before_is_redacted_too(self):
        item = rc(
            "a.x",
            ["delete"],
            before={"sk": "old"},
            change={"before_sensitive": {"sk": True}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].before["sk"], REDACTED)

    def test_unrecognised_sensitive_shape_fails_closed(self):
        # 结构对不上时按敏感处理：漏打码的代价远大于多打码。
        item = rc(
            "a.x",
            ["create"],
            after={"a": "plain"},
            change={"after_sensitive": "not-a-mapping"},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].after, REDACTED)

    def test_list_sensitive_elementwise(self):
        item = rc(
            "a.x",
            ["create"],
            after={"keys": ["public", "private"]},
            change={"after_sensitive": {"keys": [False, True]}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].after["keys"], ["public", REDACTED])

    def test_list_length_mismatch_fails_closed(self):
        item = rc(
            "a.x",
            ["create"],
            after={"keys": ["a", "b"]},
            change={"after_sensitive": {"keys": [False]}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].after["keys"], REDACTED)


class RiskTests(unittest.TestCase):
    def test_delete_and_replace_are_high(self):
        plan = from_terraform(
            tf([rc("a.x", ["delete"]), rc("a.y", ["delete", "create"])]),
            platform="aliyun",
            env="prod",
        )
        self.assertEqual([c.risk for c in plan.changes], [RISK_HIGH, RISK_HIGH])
        self.assertEqual(len(plan.high_risk), 2)

    def test_plain_create_is_low(self):
        plan = from_terraform(tf([rc("a.x", ["create"])]), platform="aliyun", env="prod")
        self.assertEqual(plan.changes[0].risk, RISK_LOW)

    def test_access_resources_are_escalated(self):
        # 权限类资源的变更要更严格审批，这条编码进风险分级而不是靠人眼认。
        plan = from_terraform(
            tf(
                [
                    rc("a.x", ["create"], type_="alicloud_ram_policy"),
                    rc("a.y", ["update"], type_="volcengine_iam_role"),
                    rc("a.z", ["update"], type_="alicloud_instance"),
                ]
            ),
            platform="aliyun",
            env="prod",
        )
        self.assertEqual([c.risk for c in plan.changes], [RISK_MEDIUM, RISK_HIGH, RISK_MEDIUM])


class PlanShapeTests(unittest.TestCase):
    def test_summary_counts_every_action(self):
        plan = from_terraform(
            tf([rc("a.x", ["create"]), rc("a.y", ["create"]), rc("a.z", ["delete"])]),
            platform="volcano",
            env="dev",
        )
        self.assertEqual(plan.summary["create"], 2)
        self.assertEqual(plan.summary["delete"], 1)
        self.assertEqual(plan.summary["update"], 0)

    def test_empty_resource_changes_is_an_empty_plan(self):
        plan = from_terraform(tf([]), platform="aliyun", env="prod")
        self.assertTrue(plan.empty)

    def test_missing_resource_changes_is_an_error_not_an_empty_plan(self):
        # 「无变更 + rc 0」会让流水线放行，所以键缺失必须报错而不是当空计划。
        with self.assertRaises(PlanParseError):
            from_terraform({"format_version": "1.2"}, platform="aliyun", env="prod")

    def test_state_document_is_rejected_with_a_clear_message(self):
        # `terraform show -json` 不带 planfile 输出的是 state，很常见的手滑。
        doc = {"format_version": "1.0", "values": {"root_module": {}}}
        with self.assertRaises(PlanParseError) as ctx:
            from_terraform(doc, platform="aliyun", env="prod")
        self.assertIn("state", str(ctx.exception))

    def test_errored_plan_is_blocked(self):
        plan = from_terraform(
            tf([rc("a.x", ["create"])], errored=True), platform="aliyun", env="prod"
        )
        self.assertTrue(plan.blocked)

    def test_unexpected_format_version_blocks_not_just_warns(self):
        # 字段布局可能已变 → 敏感标记位置也可能变 → 打码是否生效无法保证。
        doc = tf([rc("a.x", ["create"])])
        doc["format_version"] = "2.0"
        plan = from_terraform(doc, platform="aliyun", env="prod")
        self.assertTrue(plan.blocked)

    def test_after_unknown_is_labelled(self):
        item = rc(
            "a.x",
            ["create"],
            after={"id": None},
            change={"after_sensitive": {}, "after_unknown": {"id": True}},
        )
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertIn("apply", str(plan.changes[0].after["id"]))

    def test_to_json_is_round_trippable(self):
        plan = from_terraform(tf([rc("a.x", ["create"])]), platform="volcano", env="prod")
        data = json.loads(plan.to_json())
        self.assertEqual(data["platform"], "volcano")
        self.assertEqual(data["source"], "terraform")
        self.assertEqual(len(data["changes"]), 1)

    def test_rejects_non_object_document(self):
        with self.assertRaises(PlanParseError):
            from_terraform([], platform="aliyun", env="prod")

    def test_rejects_change_without_address(self):
        bad = {"mode": "managed", "type": "x", "change": {"actions": ["create"]}}
        with self.assertRaises(PlanParseError):
            from_terraform(tf([bad]), platform="aliyun", env="prod")

    def test_rejects_missing_change_object(self):
        with self.assertRaises(PlanParseError):
            from_terraform(
                tf([{"address": "a.x", "mode": "managed", "type": "t"}]),
                platform="aliyun",
                env="prod",
            )


if __name__ == "__main__":
    unittest.main()


class RedactionFalsyMarkerTests(unittest.TestCase):
    """假值型敏感标记曾经 fail-**open**：这组用例锁住它不再退化。

    原实现写成 `REDACTED if sensitive else value`，于是 `{}`、`[]`、`0`、`""`
    这些「非 True 但为假」的怪结构全部输出明文。审计实测复现过六种形状。
    """

    def _after(self, marker):
        item = rc("a.x", ["create"], after={"pw": "TOPSECRET"})
        item["change"]["after_sensitive"] = marker
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        return plan

    def test_falsy_container_markers_still_redact(self):
        for marker in ({"pw": {}}, {"pw": []}, {"pw": 0}, {"pw": ""}, {"pw": "no"}):
            plan = self._after(marker)
            self.assertNotIn("TOPSECRET", plan.to_json(), marker)

    def test_zero_is_not_treated_as_false(self):
        # `0 == False` 为真，所以不能用 `in (False, None)` 判断。
        plan = self._after({"pw": 0})
        self.assertEqual(plan.changes[0].after["pw"], REDACTED)

    def test_explicit_false_still_passes_value_through(self):
        plan = self._after({"pw": False})
        self.assertEqual(plan.changes[0].after["pw"], "TOPSECRET")

    def test_missing_sensitive_key_is_an_error_not_plaintext(self):
        # 整份标记缺失时原先默认「都不敏感」→ 整份 before/after 明文进审批卡片。
        item = rc("a.x", ["create"], after={"pw": "TOPSECRET"})
        del item["change"]["after_sensitive"]
        with self.assertRaises(PlanParseError) as ctx:
            from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertIn("after_sensitive", str(ctx.exception))

    def test_null_side_needs_no_marker(self):
        item = rc("a.x", ["create"], after={"ok": 1})
        del item["change"]["before_sensitive"]  # before 是 None，不需要标记
        plan = from_terraform(tf([item]), platform="aliyun", env="prod")
        self.assertIsNone(plan.changes[0].before)


class InputImmutabilityTests(unittest.TestCase):
    def test_source_document_is_not_mutated(self):
        # after_unknown 打标记曾经写回调用方的 document：二次解析拿到的是被改过的值。
        import copy

        item = rc("a.x", ["create"], after={"id": None})
        item["change"]["after_unknown"] = {"id": True}
        doc = tf([item])
        snapshot = copy.deepcopy(doc)
        from_terraform(doc, platform="aliyun", env="prod")
        self.assertEqual(doc, snapshot)

    def test_plan_is_a_read_only_snapshot(self):
        item = rc("a.x", ["create"], after={"k": "v"})
        doc = tf([item])
        plan = from_terraform(doc, platform="aliyun", env="prod")
        plan.changes[0].after["injected"] = "x"
        self.assertNotIn("injected", doc["resource_changes"][0]["change"]["after"])


class ChangeValidationTests(unittest.TestCase):
    def test_illegal_action_is_rejected_at_construction(self):
        from delivery.plan import Change

        with self.assertRaises(PlanParseError):
            Change(action="destroy", kind="t", address="a.x", name="x", risk="low")

import unittest

from delivery.plan import Change, Plan
from delivery.render import render_plan


def change(action, address="a.x", kind="alicloud_instance", risk="low"):
    return Change(
        action=action, kind=kind, address=address, name="x", risk=risk, before=None, after=None
    )


class RenderTests(unittest.TestCase):
    def test_empty_plan_says_no_change(self):
        out = render_plan(Plan(platform="aliyun", env="prod"))
        self.assertIn("无变更", out)

    def test_header_carries_platform_account_env(self):
        out = render_plan(Plan(platform="volcano", env="dev", account="1949"))
        self.assertIn("volcano / 1949 / dev", out)

    def test_counts_are_summarised_first(self):
        plan = Plan(
            platform="aliyun",
            env="prod",
            changes=(
                change("create"),
                change("create", "a.y"),
                change("delete", "a.z", risk="high"),
            ),
        )
        out = render_plan(plan)
        self.assertIn("创建 2 个", out)
        self.assertIn("删除 1 个", out)

    def test_high_risk_items_carry_consequence_not_just_a_flag(self):
        # 只标红只能让人知道危险；写清后果才让人判断得了该不该放行。
        plan = Plan(platform="aliyun", env="prod", changes=(change("delete", risk="high"),))
        out = render_plan(plan)
        self.assertIn("需要二次确认", out)
        self.assertIn("依赖它的任务会失败", out)

    def test_replace_explains_downtime(self):
        plan = Plan(platform="aliyun", env="prod", changes=(change("replace", risk="high"),))
        self.assertIn("先删后建", render_plan(plan))

    def test_detail_limit_truncates_but_reports_remainder(self):
        many = tuple(change("delete", f"a.n{i}", risk="high") for i in range(30))
        out = render_plan(Plan(platform="aliyun", env="prod", changes=many), detail_limit=5)
        self.assertIn("另有 25 项", out)

    def test_blocked_reason_is_shown(self):
        plan = Plan(
            platform="aliyun",
            env="prod",
            changes=(change("create"),),
            blocked=("计划自身报错",),
        )
        self.assertIn("已阻断", render_plan(plan))

    def test_warnings_are_shown_on_empty_plan_too(self):
        plan = Plan(platform="aliyun", env="prod", warnings=("格式版本未验证",))
        self.assertIn("格式版本未验证", render_plan(plan))


if __name__ == "__main__":
    unittest.main()

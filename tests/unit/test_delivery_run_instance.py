"""开 ECS。

这是面板第一个**会产生持续计费的**动作。所以测试的重点不是「能不能开出来」，
而是「会不会开出第二台」和「开出来了但台账不知道是哪台」。
"""

import unittest

from delivery.clouds import aliyun
from delivery.provision import AliyunExecutor, ProvisionError

CREDS = aliyun.Credentials("ak", "sk")
PARAMS = {
    "ZoneId": "cn-hangzhou-b",
    "VSwitchId": "vsw-x",
    "SecurityGroupId": "sg-x",
    "ImageId": "img-x",
    "InstanceType": "ecs.g7.large",
}


def fake(reply, *, seen=None):
    def send(url):
        if seen is not None:
            seen.append(url)
        return 200, reply

    return send


class RunInstanceTests(unittest.TestCase):
    def ex(self, transport):
        return AliyunExecutor("170406579653", CREDS, transport=transport)

    def test_it_returns_the_instance_id(self):
        got = self.ex(fake({"InstanceIdSets": {"InstanceIdSet": ["i-abc"]}})).run_instance(
            region="cn-hangzhou", params=PARAMS, name="req-1"
        )
        self.assertEqual(got, "i-abc")

    def test_amount_is_always_one_and_a_template_cannot_change_it(self):
        """模板里任何一个轴都不该能决定开几台。
        一个配错的数字轴就是一次「批了一台、开出二十台」。"""
        seen = []
        self.ex(fake({"InstanceIdSets": {"InstanceIdSet": ["i-abc"]}}, seen=seen)).run_instance(
            region="cn-hangzhou", params={**PARAMS, "Amount": "20"}, name="req-1"
        )
        self.assertIn("Amount=1", seen[0])
        self.assertNotIn("Amount=20", seen[0])

    def test_the_ticket_id_is_the_idempotency_token(self):
        """`ClientToken` 让重试同一张单不会开出第二台。

        没有它的话，超时重试 = 多一台机器在那儿跑着计费，而台账上只有一条记录。
        """
        seen = []
        self.ex(fake({"InstanceIdSets": {"InstanceIdSet": ["i-abc"]}}, seen=seen)).run_instance(
            region="cn-hangzhou", params=PARAMS, name="req-abc123"
        )
        self.assertIn("ClientToken=req-abc123", seen[0])

    def test_a_reply_without_an_instance_id_is_a_failure(self):
        """机器可能已经在开了，但台账里记不下它是哪台 —— 那比不开更糟，
        因为没人知道去哪找它。ClientToken 保证重试拿到的是同一台。"""
        for reply in (
            {},
            {"InstanceIdSets": {}},
            {"InstanceIdSets": {"InstanceIdSet": []}},
            {"InstanceIdSets": {"InstanceIdSet": [""]}},
        ):
            with self.assertRaises(ProvisionError, msg=str(reply)):
                self.ex(fake(reply)).run_instance(region="cn-hangzhou", params=PARAMS, name="req-1")

    def test_a_missing_region_is_refused_before_any_call(self):
        seen = []
        with self.assertRaises(ProvisionError):
            self.ex(fake({}, seen=seen)).run_instance(region="", params=PARAMS, name="req-1")
        self.assertEqual(seen, [], "没有地域却还是打了接口")

    def test_it_goes_to_the_regional_endpoint(self):
        """打错地域的表现不是报错，是「查不到这台机器」—— 和「没建成」长得一样。"""
        seen = []
        self.ex(fake({"InstanceIdSets": {"InstanceIdSet": ["i-x"]}}, seen=seen)).run_instance(
            region="cn-shanghai", params=PARAMS, name="req-1"
        )
        self.assertIn("ecs.cn-shanghai.aliyuncs.com", seen[0])
        self.assertIn("RegionId=cn-shanghai", seen[0])


if __name__ == "__main__":
    unittest.main()

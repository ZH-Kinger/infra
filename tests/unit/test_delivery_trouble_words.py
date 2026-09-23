"""「这一行是出了问题，还是进度播报」——`flows.TROUBLE_WORDS` / `flows.is_trouble`。

为什么这张表值得一个自己的文件
──────────────────────────────
它是退出码分级的判据（`cli_requests._sweep` 拿它把步骤输出分进 `broken` / `handled`），
而 2026-09-23 的线上事故有**两面**，这张表两面都踩过：

  · 判得太宽 → 一张办不了的单子每分钟触发一次兜底告警，人学会忽略告警；
  · 判得太窄 → **一个永远办不了的单子看起来和万事大吉一模一样**（退 0）。

后一面是这次改动的正主：`_sweep` 原先自己手写着 `("失败", "中断")`，于是两类**持续**
失败被算成干净一轮 ——「到期提醒仍然发不出去（…）」（重试稳态的行文，第二轮起就是它）
和「登录名还是没写进公司 IAM（…）」/「补写登录名出错（…）」。三条真实行文里一个
「失败」字都没有。

所以这个文件做四件事：
  1. 五个词各来一条**从 `flows.py` 摘的真实行文**，证明它认得；
  2. 真实的**进度行**一条都不许被判成问题（误判会把正常日志变成一分钟一条私聊）；
  3. `TableCoverageTests` 直接读 `flows.py` 的语法树，把所有「单号：…」行**穷举**出来，
     一条一条对答案。以后有人加了新的问题行、用了表里没有的词，这里会红 ——
     而不是等线上某张单子安静地卡上两天；
  4. 同时钉住 `_sweep` 调的是哪几个 `Flows` 方法 —— **只有它们吐的行会决定退出码**，
     多接一个进去就等于多一路没被分类的产出行。

退出码那一半（问题行 → 1 还是 3）在 `test_delivery_sweep_exit_codes.py`。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from delivery import flows

FLOWS_PY = Path(flows.__file__)

# ── 下面两张表的键是**行文模板**，不是行号 ────────────────────────────────
#
# 一开始用的是行号（`1574: "req-1：开通中断…"`）。放弃它是因为：任何人往 `flows.py`
# 中间插一段代码，整张表就**全错**，而且错法是「一大片 subtest 红 + 失败信息指向
# 不相干的行」—— 2026-09-23 加 `regrant_credential`（+142 行）时就这样红了 37 条。
# 真正的危险不是那次噪音，是它会把人训练成「红了就重新抄一遍行号」，
# 而那个动作和「认真看一眼这行算不算问题」恰好相反。
#
# 改成按行文做键之后：代码搬家 = 绿的（行文没变），**改了一个字 = 红**（旧键找不到 +
# 新模板没分类，两条都会报），新增一行 = 红。失败信息里的行号由扫描**现算**，
# 永远指着当前的源码。代价：同一句行文在两处出现时分不开（`回收失败，下次重试`
# 和 `已由其他进程处理` 各有两处）—— 无所谓，分类是按行文做的，不按出现位置。

#: 问题行模板 → 一条**填好占位符**的真实样例（用来喂 `is_trouble`）。一条都不是编的。
L_STUCK = "{}：{}中断，已标为失败"
L_RESUME_FAILED = "{}：审批通过后继续处理失败（{}）"
L_RESUME_BROKEN = "{}：审批通过后中断，继续处理失败（{}）"
L_REMIND_RETRY_FAILED = "{}：到期提醒仍然发不出去（{}）"
L_REMIND_FAILED = "{}：到期提醒发送失败"
L_REVOKE_FAILED_WHY = "{}：回收失败（{}），下次重试"
L_REVOKE_FAILED = "{}：回收失败，下次重试"
L_IAM_NOT_WRITTEN = "{}：登录名还是没写进公司 IAM（{}）"
L_IAM_ERROR = "{}：补写登录名出错（{}）"

TROUBLE_LINES = {
    L_STUCK: "req-1：开通中断，已标为失败",
    L_RESUME_FAILED: "req-1：审批通过后继续处理失败（云接口 500）",
    L_RESUME_BROKEN: "req-1：审批通过后中断，继续处理失败（已失败）",
    L_REMIND_RETRY_FAILED: "tak-1：到期提醒仍然发不出去（申请人没有飞书标识）",
    L_REMIND_FAILED: "tak-1：到期提醒发送失败",
    L_REVOKE_FAILED_WHY: "tak-1：回收失败（AccessDenied），下次重试",
    L_REVOKE_FAILED: "tak-1：回收失败，下次重试",
    L_IAM_NOT_WRITTEN: "req-1：登录名还是没写进公司 IAM（他现在登不进去：没接上公司 IAM 的写入）",
    L_IAM_ERROR: "req-1：补写登录名出错（IAM 接口 500）",
}

#: 真实进度行。判成问题的代价：定时任务每跑顺一轮就退 3 ——
#: 而 3 的含义是「有单子有问题、去待办页看」，那页上却什么都没有。
PROGRESS_LINES = {
    "{}：审批通过后停住的单子已接着处理（{}）": "req-1：审批通过后停住的单子已接着处理（已完成）",
    "{}：已提醒，{}到期": "tak-1：已提醒，还有 3 天到期",
    "{}：已由其他进程处理": "tak-1：已由其他进程处理",
    "{}：云上没有要清理的东西": "tak-1：云上没有要清理的东西",
    "{}：状态已变，本轮不清理": "tak-1：状态已变，本轮不清理",
    "{}：单子已不在": "tak-1：单子已不在",
    "{}：已删除没送达的凭证子账号 {}": "tak-1：已删除没送达的凭证子账号 tempak-abc",
    "{}：已补写登录名进公司 IAM": "req-1：已补写登录名进公司 IAM",
}

#: `_mark_revoked` / `_revoke` 的收尾行是 `{单号}：{note}`，算不算问题取决于 note。
#: 这些 note 全是正常收尾，一条都不该判成问题。
REVOKE_NOTES = (
    "tak-1：临时凭证已到期自动失效",
    "tak-1：管理员作废：临时凭证本就到点自灭，云上没有残留",
    "tak-1：到期：已删除子账号 tempak-abc 及其密钥和策略",
    "req-1：已移出 grp-oss-read；保留 grp-default",
    "req-1：已撤销 AliyunOSSReadOnlyAccess",
    "req-1：无需回收",
)


class WordTests(unittest.TestCase):
    """五个词各一条。用真实行文，不用「含有失败二字的字符串」那种同义反复。"""

    CASES = {
        "失败": TROUBLE_LINES[L_REMIND_FAILED],
        "中断": TROUBLE_LINES[L_STUCK],
        "发不出去": TROUBLE_LINES[L_REMIND_RETRY_FAILED],
        "出错": TROUBLE_LINES[L_IAM_ERROR],
        "还是没写进": TROUBLE_LINES[L_IAM_NOT_WRITTEN],
    }

    def test_each_word_is_carried_by_a_real_line(self):
        for word, line in self.CASES.items():
            with self.subTest(word):
                self.assertIn(word, line)
                self.assertTrue(flows.is_trouble(line), line)

    def test_the_table_is_exactly_these_five(self):
        """加词是好事（新的问题行要被认出来），但**删词是危险的**：删掉哪个，
        对应那类持续失败就重新变回「干净一轮」。所以整表钉死，改动必须路过这里。"""
        self.assertEqual(set(flows.TROUBLE_WORDS), set(self.CASES))
        self.assertEqual(len(flows.TROUBLE_WORDS), len(set(flows.TROUBLE_WORDS)), "有重复词")

    def test_no_word_is_empty(self):
        """空串会让 `any(word in line ...)` 恒真 —— 每一行都是问题行，
        于是每一轮都退非零。一个手滑的逗号就能做到（`("失败", "", "中断")`）。"""
        for word in flows.TROUBLE_WORDS:
            with self.subTest(word=repr(word)):
                self.assertTrue(str(word).strip(), "空词会把所有行都判成问题")

    def test_the_words_are_not_substrings_of_the_progress_vocabulary(self):
        """每个词都要在**所有**真实进度行上判假。

        单看一条「某个词不误判」不够：词是一张表，`any()` 里任何一个命中都算数。
        """
        for word in flows.TROUBLE_WORDS:
            for line in PROGRESS_LINES.values():
                with self.subTest(word=word, line=line):
                    self.assertNotIn(word, line)


class RealLineTests(unittest.TestCase):
    def test_every_real_problem_line_is_trouble(self):
        for template, line in sorted(TROUBLE_LINES.items()):
            with self.subTest(template):
                self.assertTrue(flows.is_trouble(line), f"flows 的问题行没被认出来：{template}")

    def test_no_real_progress_line_is_trouble(self):
        for template, line in sorted(PROGRESS_LINES.items()):
            with self.subTest(template):
                self.assertFalse(flows.is_trouble(line), f"flows 的进度行被误判：{template}")

    def test_a_normal_revoke_note_is_not_trouble(self):
        """回收成功的收尾行不是问题行。

        这一条格外要紧：`revoke_expired` 这一步是 `urgent=True`，它的问题行直接退 1、
        直接惊动人。误判在这里的后果不是多一条日志，是**每收掉一张到期凭证就私聊一次**。
        """
        for line in REVOKE_NOTES:
            with self.subTest(line):
                self.assertFalse(flows.is_trouble(line), line)

    def test_the_two_wordings_of_the_same_failure_are_both_caught(self):
        """同一件事的两种行文都要认出来。

        「到期提醒发送失败」是第一轮，「到期提醒仍然发不出去（…）」是第二轮起的稳态；
        sweep 一分钟一轮，所以线上绝大多数轮次打的是后者 —— 只认前者等于什么都没认。
        """
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[L_REMIND_FAILED]))
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[L_REMIND_RETRY_FAILED]))


class InputTests(unittest.TestCase):
    """判据本身被喂了奇怪的东西时不许炸：它跑在 sweep 的主循环里，
    抛异常就等于这一步之后的行全不看了（而 `except` 在更外层，整步会被记成 broken）。"""

    def test_an_empty_line_is_not_trouble(self):
        self.assertFalse(flows.is_trouble(""))

    def test_a_non_string_is_coerced_instead_of_raising(self):
        for junk in (None, 0, [], {"id": "req-1"}):
            with self.subTest(repr(junk)):
                self.assertFalse(flows.is_trouble(junk))

    def test_a_non_string_carrying_the_word_still_counts(self):
        """`str()` 之后含问题词就算 —— 宁可多报一条，也别因为某一步改成返回别的类型
        就把整类失败漏掉。"""
        self.assertTrue(flows.is_trouble(["req-1：回收失败，下次重试"]))

    def test_the_word_is_matched_anywhere_in_the_line(self):
        """子串匹配，不锚定位置：问题词在 flows 的行文里有时在中间
        （`审批通过后中断，继续处理失败（…）`），有时在结尾（`到期提醒发送失败`）。"""
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[L_RESUME_BROKEN]))
        self.assertTrue(flows.is_trouble("失败"))

    def test_the_state_change_line_is_not_trouble(self):
        """`_sweep` 自己打的那行状态流转 `{单号}：{旧状态} → {新状态}`。

        两件事：
        · 它压根不过 `is_trouble`（在 pending 同步那一段直接 `print`），
          所以流转本身永远不影响退出码 —— 这是对的，「待审批 → 已拒绝」不是故障；
        · 而且它打的是 `tickets.py` 里的**状态 id**（`failed` / `submit_failed`），
          不是中文 label。哪天有人图好看换成 `t.LABELS`，「开通失败」「提交失败」
          就会让每一条流转行都含「失败」二字 —— 那时这条注释是唯一的线索。
        """
        from delivery import tickets as tickets_mod

        line = f"req-1：{tickets_mod.PENDING} → {tickets_mod.FAILED}"
        self.assertFalse(flows.is_trouble(line), line)
        self.assertTrue(flows.is_trouble(f"req-1：{tickets_mod.LABELS[tickets_mod.FAILED]}"))

    def test_a_data_field_that_happens_to_contain_the_word_also_counts(self):
        """**已知的、刻意接受的误判方向**：判据是整行子串匹配，所以云上的用户组名、
        错误原文里带「失败」二字时，一条正常的收尾行也会被算成问题。

        接受它是因为两个方向不对称：多判一条 → 退 3（待办页上没东西，最多让人白看一眼）
        或者退 1（多一条私聊，有 6 小时冷却兜着）；少判一条 → 单子安静地卡死。
        这些字段都是管理员配的模板 / 云返回的错误，不是申请人能随手填的自由文本，
        所以拿不到「构造一行假告警」那种价值。

        记在这里是为了以后有人看到一条莫名其妙的退 3 时，能直接找到原因。
        """
        self.assertTrue(flows.is_trouble("req-1：已移出 grp-失败演练"))


class TableCoverageTests(unittest.TestCase):
    """直接读 `flows.py` 的语法树，把所有「单号：…」行穷举出来，一条一条对答案。

    这组是**面向未来**的：以后谁加了一条新的问题行（比如「…没能撤销」），
    而它用的词不在 `TROUBLE_WORDS` 里，这里会红并且点名那一行。
    没有它的话，新的一类持续失败会像这次一样，安安静静地退 0。

    键是行文、不是行号（理由见文件上方那段注释）。所以：

        往 flows.py 中间插代码、整段搬家  → 绿（行文没变，不该有人被惊动）
        改了一行的措辞                    → 红两条（旧键没了 + 新模板没分类）
        新增一条产出行                    → 红一条，失败信息里带**现算**的行号
    """

    @staticmethod
    def ticket_lines() -> dict:
        """`{渲染后的模板: [源码行号, …]}`。f-string 的插值位统一渲染成 `{}`。

        行号只用来在失败信息里指路 —— **现扫现算**，永远指着当前的源码，
        不存进表里（存了就会过期，而过期的指路比没有更糟）。
        """
        tree = ast.parse(FLOWS_PY.read_text(encoding="utf-8"))

        def render(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(
                    v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
                )
            return None

        out: dict = {}
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Return) and node.value is not None:
                text = render(node.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and node.args
            ):
                text = render(node.args[0])
            # 「单号：…」才是会被 sweep 打印、被分级的那种行
            if text and text.startswith("{}："):
                out.setdefault(text, []).append(node.lineno)
        return out

    #: 收尾行 `{单号}：{note}` —— 算不算问题取决于 note，单看模板判不了，
    #: 对应的 note 在 `REVOKE_NOTES` 里逐条测。
    NOTE_PASSTHROUGH = "{}：{}"

    def test_every_ticket_line_in_flows_has_been_classified(self):
        lines = self.ticket_lines()
        self.assertGreater(len(lines), 15, "语法树没扫出东西，八成是 flows.py 的写法变了")
        decided = set(TROUBLE_LINES) | set(PROGRESS_LINES)
        for template, at in sorted(lines.items()):
            if template == self.NOTE_PASSTHROUGH:
                continue
            where = "、".join(f"flows.py:{n}" for n in at)
            with self.subTest(template):
                self.assertIn(
                    template,
                    decided,
                    f"{where} 是新的一行「{template}」，还没人决定它算不算问题行。"
                    "把它填进 TROUBLE_LINES（同时确认 TROUBLE_WORDS 认得出来）"
                    "或 PROGRESS_LINES。定时任务根本不会打印它 —— 也请填进 PROGRESS_LINES "
                    "并在那里写一句为什么，这张表要的是「每一行都有人看过」",
                )

    def test_no_classified_wording_has_gone_stale(self):
        """反过来的一边：表里的每一条都得在 `flows.py` 里真的找得到。

        找不到 = 要么那行被删了、要么**措辞被改了**。后者才是要紧的：改措辞的人
        多半没想过判据这回事，而「到期提醒发送失败」改成「到期提醒没发出去」这种
        看着无害的润色，恰恰会让这一类持续失败重新变回「干净一轮」。

        （配合上一条一起看：改一个字会红两条 —— 这里报「旧的没了」，
        那里报「新的没分类」。两条凑起来就是一句完整的话。）
        """
        lines = self.ticket_lines()
        for template in sorted({**TROUBLE_LINES, **PROGRESS_LINES}):
            with self.subTest(template):
                self.assertIn(
                    template,
                    lines,
                    f"表里记着「{template}」，但 flows.py 里已经没有这一行了 ——"
                    "措辞改了就得重新判一次它算不算问题行，别直接把这条删掉",
                )

    def test_each_sample_is_an_instance_of_its_own_template(self):
        """每条样例都要是它那个模板填出来的 —— 样例是拿去喂 `is_trouble` 的，
        它要是和模板对不上，这张表就是在给一句**根本不存在的行文**背书。

        比的是模板里那几段字面量（插值位填了什么无所谓）。
        """
        for template, sample in sorted({**TROUBLE_LINES, **PROGRESS_LINES}.items()):
            with self.subTest(template):
                chunks = [c for c in template.split("{}") if c.strip("：")]
                self.assertTrue(chunks, f"只剩插值位了：{template}")
                for chunk in chunks:
                    self.assertIn(
                        chunk.strip("："),
                        sample,
                        f"样例「{sample}」不是模板「{template}」填出来的",
                    )

    #: `_sweep` 会调的 `Flows` 方法。**这张表决定了上面两张表要覆盖谁** ——
    #: 只有这几个方法吐的行会过 `is_trouble`、会决定退出码。
    SWEEP_USES = {
        "store",  # 读申请单存储
        "sync",  # 逐张同步审批
        "recover_stuck",
        "resume_approved",
        "revoke_expired",
        "retry_iam_writes",
        "remind_expiring",
    }

    def test_the_sweep_still_calls_exactly_these_flows_methods(self):
        """`_sweep` 用到的 `Flows` 方法就这几个，多一个就意味着**多一路产出行没被分类**。

        为什么值得单独一条：上面那张覆盖表扫的是 `flows.py` 里所有「单号：…」行，
        但真正要紧的是「**会被定时任务打印出来**的那些」。哪天有人把一个新方法
        接进 `steps`（比如把「改凭证」做成自动重试），它的返回行立刻开始参与退出码，
        而这个文件不会有任何反应 —— 除非这条红。

        实例：`Flows.regrant_credential`（2026-09-23 加）**不在**这里。它是管理员点出来的
        同步调用，失败直接抛 `FlowError` 给接口，不返回「单号：…」行、也不进 `steps`；
        它写的那三条事件（`cred_regrant_requested` / `_failed` / `_done`）是 `store.update`
        的 note，从来不过 `is_trouble`。所以它不该出现在上面两张表里 ——
        这条用例就是那个判断的依据，也是它哪天变了的警报。
        """
        from delivery import cli_requests

        tree = ast.parse(Path(cli_requests.__file__).read_text(encoding="utf-8"))
        sweep = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_sweep"
        )
        used = {
            n.attr
            for n in ast.walk(sweep)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "flows"
        }
        self.assertEqual(
            used,
            self.SWEEP_USES,
            "定时任务用到的 Flows 方法变了：新接进来的那个会吐什么行？"
            "把它的产出行填进 TROUBLE_LINES / PROGRESS_LINES，再更新这张表",
        )

    def test_the_regrant_notes_would_be_classified_correctly_if_they_ever_got_there(self):
        """`regrant_credential` 那三条 note 今天不过 `is_trouble`（见上一条），
        但万一哪天有人把改凭证接进定时任务，它们的措辞**恰好**是对的：

            改凭证策略失败：…                      → 问题行（「失败」）
            准备改凭证策略（…）                    → 进度行
            已改凭证策略（…）。AK 不变，对方不用改配置 → 进度行

        钉住它有两个用处：接线的人少踩一次坑；而万一有人把「已改凭证策略」
        润色成「改凭证策略出错前的最后一步」这种带问题词的说法，这里会红。
        """
        self.assertTrue(flows.is_trouble("req-1：改凭证策略失败：AccessDenied"))
        for progress in (
            "req-1：准备改凭证策略（到期 2026-10-01 → 2026-11-01）",
            "req-1：已改凭证策略（加 download）。AK 不变，对方不用改配置",
        ):
            with self.subTest(progress):
                self.assertFalse(flows.is_trouble(progress))

    def test_every_word_in_the_table_is_earning_its_keep(self):
        """表里的每个词都要有至少一条真实行文在用。

        没人用的词是纯风险：它只会在别的地方意外命中（比如某个用户组名里有它），
        把正常的一轮判成有问题。
        """
        source = FLOWS_PY.read_text(encoding="utf-8")
        for word in flows.TROUBLE_WORDS:
            with self.subTest(word):
                self.assertIn(word, source, f"`{word}` 在 flows.py 里一条行文都不对应")
                self.assertTrue(
                    any(word in line for line in TROUBLE_LINES.values()),
                    f"`{word}` 没有对应的真实问题行样本",
                )


if __name__ == "__main__":
    unittest.main()

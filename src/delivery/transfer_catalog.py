"""数据迁移能选哪些桶：从两朵云上实时拉，不靠人手维护。

为什么不写死在模板里
────────────────────
一开始是手工贴进 `request-templates.json` 的。那份清单**第二天就是错的**：
有人删了桶，申请页还让人选，提交之后才在搬运那一步炸；有人建了桶，申请页里没有，
他只能来问一句「为什么没有我的桶」。这两种都不会有人报 bug，只会让人觉得面板不准。

所以清单由定时任务从 `ListBuckets` 重新生成，和面板别的数据一个节奏。

清单同时是**白名单**
────────────────────
`flows._validate_transfer` 拿它挡「这个桶不在可申请的范围里」。所以这里排掉的桶
不只是「申请页上看不见」，而是**提交不了** —— 这正是我们要的：
镜像仓库、机器学习平台自建的那些桶，没有人应该往里搬数据。

同名撞车
────────
模板的桶清单只有 `{name, region}`，**没有云的标识**。而同一个名字两朵云都可能有
（`wuji-ego-processed` 就是：阿里那个刚被删掉，火山上海那个还在）。
撞名的两边都不收 —— 收任何一个都意味着「按名字查地域」会静默取错，
而那种错的表现是数据搬到了另一朵云的另一个地域，一路都不报错。
"""

from __future__ import annotations

from typing import Iterable, Optional

#: 这些桶不是用来放数据的，不该出现在迁移的两端。**按前缀排，理由写在旁边。**
_SKIP_PREFIX = (
    ("cri-", "容器镜像仓库，不是数据"),
    ("ml-platform-auto-created-", "机器学习平台自己建的，往里写会动到平台的东西"),
    ("oss-pai-", "PAI 自建"),
    ("h2r-dlc-", "DLC 自建"),
)
#: 整名匹配的
_SKIP_EXACT = (
    ("las-datastore", "LAS 自己的库"),
    ("data-infra-emr-log-hgh", "EMR 日志"),
)


def _why_skip(name: str) -> str:
    """这个桶为什么不收。收就返回空串。"""
    for prefix, why in _SKIP_PREFIX:
        if name.startswith(prefix):
            return why
    for exact, why in _SKIP_EXACT:
        if name == exact:
            return why
    return ""


def _region(raw: object) -> str:
    """地域归一。

    **OSS 的 ListBuckets 返回的是带 `oss-` 前缀的写法**（`oss-cn-hangzhou`），
    而引擎那边要的是裸地域（`cn-hangzhou`，自己去拼域名）。
    不归一的话会拼出 `oss-oss-cn-hangzhou.aliyuncs.com` —— 这个错在别处踩过。
    """
    got = str(raw or "").strip()
    return got[4:] if got.startswith("oss-") else got


def pick(oss_rows: Iterable, tos_rows: Iterable) -> tuple:
    """两朵云的桶清单 → `(能选的 [{name, region}], 排掉的 [(名字, 理由)])`。

    纯函数，不碰网络。排掉的那一份要留着打日志：一个桶悄悄从申请页上消失，
    比它一直在那儿更让人困惑。
    """
    seen: dict = {}
    dropped: list = []

    def take(rows, cloud: str) -> None:
        for row in rows or ():
            name = str((row or {}).get("name") or "").strip()
            if not name:
                continue
            why = _why_skip(name)
            if why:
                dropped.append((name, why))
                continue
            region = _region((row or {}).get("region") or (row or {}).get("location"))
            if not region:
                dropped.append((name, "没拿到地域"))
                continue
            if name in seen and seen[name] != region:
                # 两朵云同名。清单表达不出是哪朵，两边都不能收 —— 见模块开头
                dropped.append((name, f"两朵云都有这个名字（{seen[name]} / {region}），无法区分"))
                seen[name] = None
                continue
            if name not in seen:
                seen[name] = region
        del cloud

    take(oss_rows, "aliyun")
    take(tos_rows, "volcano")
    rows = [{"name": n, "region": r} for n, r in sorted(seen.items()) if r]
    return rows, sorted(dropped)


def apply_to(templates: dict, rows: list, *, template_id: str = "oss-move") -> tuple:
    """把清单写进模板。返回 `(加了哪些, 删了哪些)`。

    **一个桶都拉不到时不动模板。** 那多半是凭证过期或者接口抖了，
    把清单清空的后果是所有迁移申请都提交不了，而且页面上看不出为什么。
    """
    items = templates.get("templates")
    tpl = next((x for x in items or () if x.get("id") == template_id), None)
    if tpl is None:
        raise KeyError(f"模板里没有 {template_id}")
    if not rows:
        raise ValueError("一个桶都没拉到，这次不动模板（多半是凭证或接口的问题）")
    before = {b.get("name") for b in tpl.get("buckets") or []}
    after = {b["name"] for b in rows}
    tpl["buckets"] = rows
    return sorted(after - before), sorted(before - after)


def summary(
    added: list, removed: list, dropped: list, total: int, counts: Optional[tuple] = None
) -> str:
    """一行日志。没变化也要说一声 —— 「什么都没打印」和「任务没跑」分不出来。

    **每朵云各列到几个要单独报。** 阿里的 ListBuckets 结果会按调用者的权限过滤
    （实测采集身份看到 24 个、另一把看到 26 个，差的两个既不在清单也不在排除列表里）。
    那种「少了但没人知道」只有把数目摆出来、让人对着控制台看一眼才发现得了。
    """
    parts = [f"迁移可选桶 {total} 个"]
    if counts:
        parts.append(f"云上列到 阿里 {counts[0]} / 火山 {counts[1]}")
    if added:
        parts.append(f"新增 {len(added)}：{'、'.join(added[:6])}")
    if removed:
        parts.append(f"移除 {len(removed)}：{'、'.join(removed[:6])}")
    if not added and not removed:
        parts.append("没变化")
    if dropped:
        parts.append(f"排除 {len(dropped)} 个（镜像仓库/平台自建/撞名）")
    return "；".join(parts)


def live(
    *,
    aliyun_creds,
    volcano_creds,
    oss_region: str = "oss-cn-hangzhou",
    tos_endpoint: str = "tos-cn-shanghai.volces.com",
    tos_region: str = "cn-shanghai",
    transport=None,
) -> tuple:
    """真去两朵云上拉一次。返回 `(能选的, 排掉的, 出的问题, (阿里数, 火山数))`。

    **两朵云分开 try。** 一朵拉不到不该让另一朵的更新也作废 —— 那会让一次
    火山侧的接口抖动，把阿里那边刚建的桶也挡在申请页外面。
    """
    problems = []
    oss_rows: list = []
    tos_rows: list = []
    try:
        from .clouds import oss as oss_mod

        oss_rows = oss_mod.list_buckets(region=oss_region, creds=aliyun_creds, transport=transport)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"阿里 OSS 列桶失败：{str(exc)[:160]}")
    try:
        tos_rows = list_tos(volcano_creds, endpoint=tos_endpoint, region=tos_region)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"火山 TOS 列桶失败：{str(exc)[:160]}")
    rows, dropped = pick(oss_rows, tos_rows)
    return rows, dropped, problems, (len(oss_rows), len(tos_rows))


#: 列 TOS 桶用采集身份（`panel-collector`）—— 列桶是盘点，盘点就是它的活。
#:
#: 一度为它单开过一把只有 `tos:ListBuckets` 的身份（`panel-tos-lister`），
#: 起因是 collector 上有一条 `Deny tos:*` 挡着。但那条闸自己的注释写的是
#: 「这把身份永远不该**写**任何东西」，而列桶名是读 —— 是那条 Deny 写宽了，
#: 不是列桶该另找身份。现在 Deny 收窄成 TOS 的写动作 + `GetObject`
#: （不让它看对象内容），意图一字没动，而长期 AK 少一把要轮换。
ENV_VOLCANO = "VOLCANO"


def tos_creds(environ: Optional[dict] = None):
    """列桶用的火山凭证 = 采集身份。

    **没配就抛错，不回落到 `TOS_ACCESS_KEY`。** 那是 bot 那把宽得多的钥匙；
    回落的话这个功能会照常工作，谁都不会发现最小权限那层已经没了。
    """
    import os

    from .clouds import volcano

    env = os.environ if environ is None else environ
    ak = env.get(f"{ENV_VOLCANO}_ACCESS_KEY", "")
    sk = env.get(f"{ENV_VOLCANO}_SECRET_KEY", "")
    if not (ak and sk):
        raise RuntimeError(
            f"没配 {ENV_VOLCANO}_ACCESS_KEY / {ENV_VOLCANO}_SECRET_KEY。"
            "这里要的是采集身份（panel-collector，只读），"
            "**不要拿 TOS_ACCESS_KEY 顶替** —— 那把钥匙能做的事多得多。"
        )
    return volcano.Credentials(ak, sk)


def list_tos(creds, *, endpoint: str, region: str) -> list:
    """火山 TOS 的桶清单。返回 `[{name, region}]`。"""
    try:
        import tos
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
        raise RuntimeError("tos SDK 没装，列不了火山的桶（pip install tos）") from exc
    ak = getattr(creds, "access_key_id", "")
    sk = getattr(creds, "secret_access_key", "") or getattr(creds, "access_key_secret", "")
    if not (ak and sk):
        raise RuntimeError("没配火山的 AK/SK")
    client = tos.TosClientV2(ak, sk, endpoint, region)
    got = client.list_buckets()
    return [
        {
            "name": str(getattr(b, "name", "") or ""),
            "region": str(getattr(b, "location", "") or getattr(b, "region", "") or ""),
        }
        for b in (getattr(got, "buckets", None) or [])
    ]


def load(path: str) -> dict:
    import json
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path: str, data: dict, *, mode: Optional[int] = 0o600) -> None:
    """原子写。申请模板是面板的门面 —— 写了一半的 JSON 会让整个申请页打不开。"""
    import json
    import os
    import tempfile
    from pathlib import Path

    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tpl-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            Path(tmp).chmod(mode)
        Path(tmp).replace(target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise

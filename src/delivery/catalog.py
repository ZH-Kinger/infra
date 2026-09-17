"""申请模板目录：员工只能从这里选，不能自己填用户组、角色或策略。

模板文件 `identity/request-templates.json`（gitignored：里面有云账号 ID），格式见
`identity/request-templates.example.json`。加载时逐项校验，写错直接拒绝加载——
模板决定审批通过后往云上写什么，静默忽略一个拼错的字段可能就是多给了权限。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from . import platforms as platforms_mod
from .errors import DeliveryError

SCHEMA = "wuji-request-templates@1"

KIND_ACCOUNT = "account"
KIND_PERMISSION = "permission"
KIND_CREDENTIAL = "credential"
#: 非固定资源（ECS / RDS / 带宽包……）。
#:
#: **模板里有没有 `specs`，决定这张单子是自动开还是人工开**：
#:   有 specs → 审批通过后面板直接调云 API 建好，实例 ID 回写台账（和凭证/权限/开账号一致）
#:   没 specs → 停在「待开通」，申请人自己写规格，管理员建好后回来登记
#: 分界不是「资源危险所以人工」，而是「有没有一份机器可读、审批人批得下来的确定规格」。
#: 一句话规格（「4C16G 杭州」）调不了 API：镜像、VPC、交换机、安全组、磁盘、计费方式都缺。
#: 所以自动开的前提是模板把这些全配死成套餐，申请人只选套餐 —— 审批人批的也才是确定的东西。
KIND_RESOURCE = "resource"
KINDS = (KIND_ACCOUNT, KIND_PERMISSION, KIND_CREDENTIAL, KIND_RESOURCE)
KIND_LABELS = {
    KIND_ACCOUNT: "开账号",
    KIND_PERMISSION: "云账号权限",
    KIND_CREDENTIAL: "访问凭证",
    KIND_RESOURCE: "资源开通",
}

RISKS = ("low", "medium", "high")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_ACCOUNT = re.compile(r"^[0-9]{6,20}$")
_GROUP = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: 用户名规则的默认值：小写字母开头，只含小写字母数字点和横线
DEFAULT_USERNAME = r"^[a-z][a-z0-9.-]{1,31}$"
#: 凭证时长上限：一年。**长短期不是两种申请**，是同一个申请按时长自动选实现方式——
#: 12 小时以内用 STS 临时凭证（到点自灭），超过就建带时间窗策略的子账号发长期 AK。
#: 12 这个数不是我们定的：阿里云 AssumeRole 的 DurationSeconds 硬顶就是 43200 秒。
STS_MAX_HOURS = 12
MAX_CREDENTIAL_HOURS = 24 * 365
#: 资源申请里申请人自己写的规格 / 用途
SPEC_MAX = 500
_SPEC_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,39}$")
_SPEC_LABEL_MAX = 60
#: 选项轴的数量上限，和每轴里选项的数量上限 —— 两件事，限制不该一样。
#: 轴多了表单没法看；而「规格」这一轴列二十几种机型是正常的
_AXES_MAX = 12
_CHOICES_MAX = 60
#: 目前能自动开的资源类型。**不在表里的一律拒绝加载**，而不是配了却悄悄开不出来
RESOURCE_TYPES = ("ecs",)
_REGION_ID = re.compile(r"^[a-z0-9-]{2,32}$")
#: 模板里没填完的占位符，例如 "<待建：…>"。它长得像正常字符串，能一路传到
#: RunInstances 才报错 —— 那时审批已经过了、申请人也已经等完一轮
_PLACEHOLDER = re.compile(r"[<＜].{0,60}[>＞]")
#: 成本归属里「其他（自己填）」的固定 id
COST_OTHER = "other"
_CATEGORY_MAX = 16
#: 桶名与地域：模板白名单里的桶，申请人只能从中选
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9-]{2,32}$")
CAPS = ("list", "download", "write")
CAP_LABELS = {"list": "查看清单", "download": "下载", "write": "上传"}


class CatalogError(DeliveryError):
    """模板目录不合法。"""


@dataclass(frozen=True)
class Template:
    id: str
    kind: str
    platform: str
    account: str
    title: str
    description: str = ""
    risk: str = "low"
    #: 展示用分类（如「存储」「AI 训练」），只影响申请页的筛选，不影响开通内容
    category: str = ""
    #: permission：加入的用户组；account：新账号默认加入的用户组
    groups: tuple = ()
    #: credential：≤12 小时走 STS 时扮演的角色（留空则这个模板只发长期凭证）
    role_arn: str = ""
    #: credential：最长有效期（小时）。申请人填的时长决定用 STS 还是长期 AK
    max_hours: int = 1
    #: credential：这个模板发的凭证能干什么（list / download / write，正交）
    caps: tuple = ()
    #: credential：允许申请的桶白名单，((桶名, 地域), ...)
    buckets: tuple = ()
    #: credential：允不允许申请人把范围收窄到某个子目录
    allow_prefix: bool = True
    #: permission / resource：最长授权天数，0 = 不限（到期回收在后续阶段）
    max_days: int = 0
    #: resource：选项轴（空 = 面板不建，停在「待开通」等人工），以及自由填写时的提示
    options: tuple = ()
    spec_hint: str = ""
    #: resource：所有选项共用的基础创建参数（网络、镜像这类不让申请人选的）
    params: dict = field(default_factory=dict, compare=False)
    #: resource：成本归属可选项。空 = 这类资源不记成本归属
    cost_centers: tuple = ()
    #: resource：清单里没有时允不允许自己填。新项目、临时立项常常还没进清单，
    #: 不给填的话人只会随便挑一个最像的 —— 那比让他写清楚更糟
    cost_center_other: bool = False
    #: resource：资源类型与地域。只有带 options 的模板需要
    resource_type: str = ""
    region: str = ""
    #: account：用户名规则、是否开通控制台登录（领取一次性初始密码）
    username_pattern: str = DEFAULT_USERNAME
    console_login: bool = False
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def automatic(self) -> bool:
        """审批通过后面板自己建，还是停下来等人工。

        **现在恒为 False**：套餐（`specs`）的 schema 和参数禁用清单已经就位，但还没有
        任何代码去读 `specs[i].params` 调云 API —— 接线还没做。在那之前对前端说 True
        就是骗人：页面会写「审批通过后自动开通」，实际上单子照样停在「待开通」等管理员。
        接线时把这里改成 `bool(self.specs)`，同时 flows._EXEC_FIELDS 必须已经含 specs
        （审批人批的是那份参数，开通前要核对它没被改过）。
        """
        return False

    def hidden_axes(self, picked: Mapping) -> set:
        """按当前选择，哪些轴该隐藏 —— 不显示、不校验、也不贡献参数。

        **被隐藏的轴不能再去隐藏别的轴**：它根本没被问，它上面的那个「选择」是申请人
        凭空提交的。不逐个重算的话，构造一组「A 隐藏 B、B 隐藏 C」的提交就能让 C 不被
        问、参数退回模板默认 —— 而审批人看到的单子上压根没有 C 这一行。

        按声明顺序走一遍就够：加载期已经强制 `hidden_when` 只能引用**排在前面**的轴
        （见 `_axes`），所以轮到某一轴时，它依赖的那些轴的去留已经定了。
        """
        skip: set = set()
        for axis in self.options:
            if axis.hidden({k: v for k, v in picked.items() if k not in skip}):
                skip.add(axis.id)
        return skip

    def resolved_params(self, picked: Mapping, numbers: Optional[Mapping] = None) -> dict:
        """模板基础参数 + 各轴选中项 / 填入数字，**后者覆盖前者**。

        覆盖是有意的：基础参数写「默认这样」，轴上的选择写「这次要那样」。没有覆盖的话，
        想让某个选项关掉一个默认值就只能把默认值从基础参数里拿掉、在每个选项里重复一遍。

        传进来的值**只用来查表或当数字**，绝不会变成参数名或参数值本身 ——
        申请人能影响的永远只是「选哪个」「填多少」。
        """
        out = dict(self.params)
        numbers = numbers or {}
        skip = self.hidden_axes(picked)
        for axis in self.options:
            if axis.id in skip:
                continue
            if axis.number is not None:
                raw = numbers.get(axis.id)
                if raw is None:
                    continue
                try:
                    value = axis.number.clean(raw)
                except CatalogError:
                    continue
                if not (value == 0 and axis.number.omit_zero):
                    out[axis.number.param] = str(value)
                    out.update(dict(axis.number.with_params))
                continue
            got = axis.choice(str(picked.get(axis.id) or ""))
            if got is not None:
                out.update(got[2])
        return out

    def axis(self, axis_id: str) -> Optional[Axis]:
        return next((a for a in self.options if a.id == axis_id), None)

    def choice(self, axis_id: str, choice_id: str):
        found = self.axis(axis_id)
        return found.choice(choice_id) if found else None

    def public(self) -> dict:
        """给前端和 CLI 的字段：不含角色 ARN（员工不需要知道，也不能改）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "kind_label": KIND_LABELS[self.kind],
            "platform": self.platform,
            "account": self.account,
            "title": self.title,
            "description": self.description,
            "risk": self.risk,
            "category": self.category,
            "groups": list(self.groups),
            "max_hours": self.max_hours if self.kind == KIND_CREDENTIAL else 0,
            "sts_max_hours": STS_MAX_HOURS if self.kind == KIND_CREDENTIAL else 0,
            # 给前端算「这次会发临时凭证还是长期凭证」用。**只暴露有没有，不暴露角色 ARN**
            "sts_available": bool(self.role_arn) and self.kind == KIND_CREDENTIAL,
            "caps": list(self.caps),
            "cap_labels": [CAP_LABELS[c] for c in self.caps],
            "buckets": [{"name": n, "region": r} for n, r in self.buckets],
            "allow_prefix": self.allow_prefix if self.kind == KIND_CREDENTIAL else False,
            "max_days": self.max_days if self.kind in (KIND_PERMISSION, KIND_RESOURCE) else 0,
            # 只给前端 id 和给人看的名字。**params 绝不外传**：里面是镜像、交换机、
            # 安全组 ID，属于内网拓扑，没必要让每个申请人都看到
            "options": [
                {
                    "id": a.id,
                    "label": a.label,
                    "choices": [{"id": c, "label": cl} for c, cl, _ in a.choices],
                    "number": (
                        None
                        if a.number is None
                        else {
                            "min": a.number.min,
                            "max": a.number.max,
                            "step": a.number.step,
                            "default": a.number.default,
                            "unit": a.number.unit,
                            "omit_zero": a.number.omit_zero,
                        }
                    ),
                    "hidden_when": {k: list(v) for k, v in a.hidden_when},
                }
                for a in self.options
            ],
            "cost_centers": [{"id": i, "label": label} for i, label in self.cost_centers],
            "cost_center_other": self.cost_center_other,
            "spec_hint": self.spec_hint,
            "resource_type": self.resource_type,
            "region": self.region,
            "automatic": self.automatic,
            "username_pattern": self.username_pattern if self.kind == KIND_ACCOUNT else "",
            "console_login": self.console_login if self.kind == KIND_ACCOUNT else False,
        }


def _str(spec: dict, key: str, where: str, *, required: bool = True) -> str:
    value = spec.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise CatalogError(f"{where}：{key} 必须是非空字符串")
    return value.strip()


def _int(spec: dict, key: str, where: str, default: int, lo: int, hi: int) -> int:
    value = spec.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise CatalogError(f"{where}：{key} 必须是 {lo}–{hi} 之间的整数")
    return value


#: 每种模板认哪些字段。**按 kind 分开列**，不是合成一张大表：
#: 把 max_days 写进凭证模板、把 max_hours 写进权限模板，都是「以为限制住了其实没有」，
#: 静默忽略正是权限事故的常见开头，所以一律当成写错、拒绝加载。
_COMMON = {"id", "kind", "platform", "account", "title", "description", "risk", "category"}
_BY_KIND = {
    KIND_ACCOUNT: {"groups", "username_pattern", "console_login"},
    KIND_PERMISSION: {"groups", "max_days"},
    KIND_CREDENTIAL: {"role_arn", "max_hours", "caps", "buckets", "allow_prefix"},
    KIND_RESOURCE: {
        "max_days",
        "options",
        "params",
        "cost_centers",
        "cost_center_other",
        "spec_hint",
        "resource_type",
        "region",
    },
}


def _buckets(spec: dict, where: str) -> tuple:
    """`[{"name": "wuji-data", "region": "cn-hangzhou"}, ...]` → ((名, 地域), ...)。

    地域是模板写死的，不现场探测：探测要给执行身份放开 OSS 读权限，而这个信息
    只是拼给使用方的 endpoint —— 为了拼一行字扩权不划算。写错了使用方会拿到
    「must be addressed using the specified endpoint」的 403，改模板即可。
    """
    items = spec.get("buckets", [])
    if not isinstance(items, list) or not items:
        raise CatalogError(f"{where}：buckets 必须是非空数组，列出这个模板允许申请的桶")
    out = []
    for item in items:
        if not isinstance(item, dict) or sorted(item) != ["name", "region"]:
            raise CatalogError(f'{where}：buckets 每项必须是 {{"name": ..., "region": ...}}')
        name, region = str(item["name"]), str(item["region"])
        if not _BUCKET_NAME.match(name):
            raise CatalogError(f"{where}：桶名不合法 {name[:64]!r}")
        if not _REGION.match(region):
            raise CatalogError(f"{where}：地域不合法 {region[:64]!r}（例如 cn-hangzhou）")
        if region.startswith("oss-"):
            # endpoint 是 oss-<region>.aliyuncs.com 拼出来的。这里再带一次前缀就会拼成
            # oss-oss-cn-hangzhou.aliyuncs.com，使用方拿到凭证连不上、以为凭证是坏的
            raise CatalogError(f"{where}：地域写裸名 {region[4:]!r}，不要带 oss- 前缀")
        out.append((name, region))
    names = [n for n, _ in out]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise CatalogError(f"{where}：buckets 里桶名重复 {'、'.join(dup)}")
    return tuple(out)


#: 套餐里**不许出现**的参数。模板是管理员写的、和代码一样可信，但这几个的后果值得单独挡一道。
#:
#: 提权面：
#:   RamRoleName / IamRoleName  给实例绑云上角色 —— 谁能登上这台机器，谁就能从实例元数据
#:                              服务直接取到那个角色的临时凭证。等于把一条提权通道写进了
#:                              「申请一台机器」这个动作里。真要绑角色应该是一次单独的、
#:                              写明角色名的审批。（授权层还有一道：**不给执行身份
#:                              `ram:PassRole`** —— 阿里官方明说绑角色必须有它。）
#:   UserData                   Base64 的开机脚本，以 root 执行。
#: 交付面（这些由面板自己填，模板填了会被覆盖或产生歧义）：
#:   Password / KeyPairName     登录凭据每台现场生成、走审批评论下发，不能在模板里写死一个
#:                              全员共用的
#:   ClientToken                幂等令牌，必须由申请单号派生，模板写死等于所有单子共用一个
#:   Amount / Count             一次只建一台
#:   InstanceName               带申请单号，便于对账
_SPEC_FORBIDDEN = {
    "ramrolename",
    "iamrolename",
    "userdata",
    "password",
    "keypairname",
    "clienttoken",
    "amount",
    "count",
    "instancename",
    # 阿里的 AutoPay **默认就是 true**：配成包年包月时提交即扣整期费用，而且官方明说
    # autoPay=true 时不需要 bss 权限 —— 授权层拦不住，只能在这里拦
    "autopay",
}
#: 包年包月会**立刻扣款**，而且火山的包年包月实例「到期或退订前不支持删除」——
#: 误建一台就是钱扣了、机器删不掉、只能人工退订。自动开通一律按量付费。
_SPEC_PREPAID = {"prepaid", "prepay", "subscription"}


@dataclass(frozen=True)
class NumberField:
    """一个轴上让申请人填数字（磁盘大小这种连续值）。

    为什么不给下拉：磁盘大小是连续的，列成 40/100/200/500 只是把「填多少」这件事
    换成「在我们猜的几档里挑一个最接近的」。离散且有限的东西（机型、镜像）才该选。
    值只当整数用，边界由模板定，前端和服务端各校验一遍。
    """

    param: str
    min: int
    max: int
    step: int
    default: int
    unit: str = ""
    #: 填 0 时整组参数都不发（「不要数据盘」）。没有这个的话会发出 Size=0 这种废参数
    omit_zero: bool = False
    #: 跟这个数字绑在一起的参数（数据盘的类型、是否随机器删除）。**只在数字非 0 时发出**
    #: —— 放进模板的基础参数里的话，「不要数据盘」时它们还在，会开出一块默认大小的盘
    with_params: tuple = ()

    def clean(self, raw: object) -> int:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise CatalogError("必须是整数")
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise CatalogError("必须是整数") from None
        if value == 0 and self.omit_zero:
            return 0
        if not self.min <= value <= self.max:
            raise CatalogError(f"要在 {self.min}–{self.max} 之间")
        if self.step > 1 and value % self.step:
            raise CatalogError(f"要是 {self.step} 的整数倍")
        return value


@dataclass(frozen=True)
class Axis:
    """申请资源时的一个选择维度。要么是一组选项，要么是一个数字输入，不会两者都有。"""

    id: str
    label: str
    #: ((选项 id, 显示名, 云参数), ...)
    choices: tuple = ()
    number: Optional[NumberField] = None
    #: ((别的轴 id, (那个轴的选项 id, ...)), ...)：命中时这一轴隐藏、不校验、不出参数
    hidden_when: tuple = ()

    def hidden(self, picked: Mapping) -> bool:
        return any(str(picked.get(k) or "") in v for k, v in self.hidden_when)

    def choice(self, choice_id: str):
        return next((c for c in self.choices if c[0] == choice_id), None)


def _params(raw: object, where: str) -> dict:
    """校验一组云 API 创建参数。**全部来自模板，一个字节都不来自申请人。**

    申请人只能选「哪个选项」，选项对应什么参数由模板定死。让申请人影响创建参数，
    等于把云账号的 API 开放给全员。
    """
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}：params 必须是对象")
    clean = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool):
            raise CatalogError(f"{where}：params 只能是字符串或整数")
        if k.lower().replace("_", "").replace(".", "") in _SPEC_FORBIDDEN:
            raise CatalogError(f"{where}：不允许配 {k}（会给实例带上额外凭证、开机脚本或预付费）")
        if str(v).lower() in _SPEC_PREPAID:
            raise CatalogError(f"{where}：{k}={v} 是预付费，会立刻扣款；自动开通只支持按量付费")
        if _PLACEHOLDER.search(str(v)):
            raise CatalogError(f"{where}：{k} 还是占位符 {v!r}，填上真实值再用")
        clean[k] = str(v)
    return clean


def _number(raw: object, where: str) -> NumberField:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}：number 必须是对象")
    unknown = sorted(
        set(raw) - {"param", "min", "max", "step", "default", "unit", "omit_zero", "with_params"}
    )
    if unknown:
        raise CatalogError(f"{where}：number 里不认识的字段 {'、'.join(unknown)}")
    param = str(raw.get("param") or "")
    if not param:
        raise CatalogError(f"{where}：number 要指定 param（这个数字填进哪个云参数）")
    if param.lower().replace("_", "").replace(".", "") in _SPEC_FORBIDDEN:
        raise CatalogError(f"{where}：number 不允许写进 {param}")
    extra = raw.get("with_params") or {}
    if not isinstance(extra, dict):
        raise CatalogError(f"{where}：number.with_params 必须是对象")
    nums = {}
    for key, lo, hi in (("min", 0, 1 << 20), ("max", 1, 1 << 20), ("step", 1, 1024)):
        v = raw.get(key, 1 if key == "step" else None)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise CatalogError(f"{where}：number.{key} 必须是 {lo}–{hi} 的整数")
        nums[key] = v
    if nums["min"] > nums["max"]:
        raise CatalogError(f"{where}：number.min 不能大于 number.max")
    omit_zero = raw.get("omit_zero", False)
    if not isinstance(omit_zero, bool):
        raise CatalogError(f"{where}：number.omit_zero 必须是 true / false")
    default = raw.get("default", nums["min"])
    if not isinstance(default, int) or isinstance(default, bool):
        raise CatalogError(f"{where}：number.default 必须是整数")
    if not (nums["min"] <= default <= nums["max"] or (default == 0 and omit_zero)):
        raise CatalogError(f"{where}：number.default 要在 min–max 之间")
    return NumberField(
        param=param,
        min=nums["min"],
        max=nums["max"],
        step=nums["step"],
        default=default,
        unit=str(raw.get("unit") or ""),
        omit_zero=omit_zero,
        with_params=tuple(sorted(_params(extra, f"{where} number.with_params").items())),
    )


def _options(spec: dict, where: str) -> tuple:
    """选择维度：`[{"id","label","choices"|"number"[,"hidden_when"]}, ...]`。

    为什么是**多轴**而不是一维套餐：一台机器要定的不止规格 —— 还有系统盘、数据盘、
    公网带宽。一维套餐要把所有组合列成笛卡尔积，十几项就爆炸，改一档盘大小要动所有条目。

    一个轴要么给一组选项（离散的：机型、镜像、安全组），要么让人填数字（连续的：磁盘大小）。
    申请人能影响的永远只是「选哪个」或「填多少」，创建参数本身一个字节都来自模板。
    """
    items = spec.get("options", [])
    if items in ((), [], None):
        return ()
    if not isinstance(items, list):
        raise CatalogError(f"{where}：options 必须是数组")
    if len(items) > _AXES_MAX:
        raise CatalogError(f"{where}：options 最多 {_AXES_MAX} 轴")
    out: list = []
    for axis in items:
        if not isinstance(axis, dict):
            raise CatalogError(f"{where}：options 每项必须是对象")
        unknown = sorted(set(axis) - {"id", "label", "choices", "number", "hidden_when"})
        if unknown:
            raise CatalogError(f"{where}：选项轴里不认识的字段 {'、'.join(unknown)}")
        aid, alabel = str(axis.get("id") or ""), str(axis.get("label") or "").strip()
        if not _SPEC_ID.match(aid):
            raise CatalogError(f"{where}：选项轴 id 只能含小写字母、数字、点和横线：{aid[:40]!r}")
        if not alabel or len(alabel) > _SPEC_LABEL_MAX:
            raise CatalogError(f"{where}：选项轴 {aid} 的 label 不能为空")
        has_choices, has_number = "choices" in axis, "number" in axis
        if has_choices == has_number:
            raise CatalogError(f"{where}：选项轴 {aid} 要么给 choices，要么给 number，不能都给")

        got: tuple = ()
        number = None
        if has_number:
            number = _number(axis["number"], f"{where} 选项轴 {aid}")
        else:
            choices = axis["choices"]
            if not isinstance(choices, list) or not choices:
                raise CatalogError(f"{where}：选项轴 {aid} 至少要有一个选项")
            if len(choices) > _CHOICES_MAX:
                raise CatalogError(f"{where}：选项轴 {aid} 最多 {_CHOICES_MAX} 个选项")
            built = []
            for c in choices:
                if not isinstance(c, dict) or sorted(c) != ["id", "label", "params"]:
                    raise CatalogError(
                        f'{where}：选项轴 {aid} 的每个选项必须是 {{"id", "label", "params"}}'
                    )
                cid, clabel = str(c["id"]), str(c["label"]).strip()
                if not _SPEC_ID.match(cid):
                    raise CatalogError(f"{where}：选项 id 不合法：{cid[:40]!r}")
                if not clabel or len(clabel) > _SPEC_LABEL_MAX:
                    raise CatalogError(f"{where}：选项 {aid}/{cid} 的 label 不能为空")
                built.append((cid, clabel, _params(c["params"], f"{where} 选项 {aid}/{cid}")))
            ids = [c[0] for c in built]
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise CatalogError(f"{where}：选项轴 {aid} 里 id 重复 {'、'.join(dup)}")
            got = tuple(built)

        # hidden_when 引用的轴必须排在前面 —— 不然是循环依赖，前端也没法按顺序显示
        raw = axis.get("hidden_when") or {}
        if not isinstance(raw, dict):
            raise CatalogError(f"{where}：选项轴 {aid} 的 hidden_when 必须是对象")
        hidden = []
        known = {a.id: a for a in out}
        for other, values in raw.items():
            if other not in known:
                raise CatalogError(
                    f"{where}：选项轴 {aid} 的 hidden_when 引用了 {other}，"
                    f"它必须是前面已经出现过的轴"
                )
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise CatalogError(f"{where}：hidden_when[{other}] 必须是选项 id 数组")
            valid = {c[0] for c in known[other].choices}
            bad = sorted(set(values) - valid)
            if bad:
                raise CatalogError(
                    f"{where}：hidden_when[{other}] 里没有这些选项：{'、'.join(bad)}"
                )
            hidden.append((other, tuple(values)))
        out.append(
            Axis(id=aid, label=alabel, choices=got, number=number, hidden_when=tuple(hidden))
        )
    ids = [a.id for a in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise CatalogError(f"{where}：选项轴 id 重复 {'、'.join(dup)}")
    return tuple(out)


def _cost_centers(spec: dict, where: str) -> tuple:
    """成本归属：((id, label), ...)。也是**选不是填** —— 自由填写的成本中心对不上账。"""
    items = spec.get("cost_centers", [])
    if items in ((), [], None):
        return ()
    if not isinstance(items, list):
        raise CatalogError(f"{where}：cost_centers 必须是数组")
    out = []
    for item in items:
        if not isinstance(item, dict) or sorted(item) != ["id", "label"]:
            raise CatalogError(f'{where}：cost_centers 每项必须是 {{"id", "label"}}')
        cid, label = str(item["id"]), str(item["label"]).strip()
        if not _SPEC_ID.match(cid):
            raise CatalogError(f"{where}：成本归属 id 不合法：{cid[:40]!r}")
        if cid == COST_OTHER:
            # 「其他」是 cost_center_other 开关合成的，不能当普通选项写进清单：
            # 写进去之后查表就查得到，自填那条分支永远走不到，填的内容被丢掉
            raise CatalogError(
                f'{where}：成本归属 id 不能叫 "{COST_OTHER}"，它留给「其他（自己填）」'
            )
        if not label or len(label) > _SPEC_LABEL_MAX:
            raise CatalogError(f"{where}：成本归属 {cid} 的 label 不能为空")
        out.append((cid, label))
    ids = [c[0] for c in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise CatalogError(f"{where}：成本归属 id 重复 {'、'.join(dup)}")
    return tuple(out)


def parse_template(spec: object, index: int) -> Template:
    where = f"templates[{index}]"
    if not isinstance(spec, dict):
        raise CatalogError(f"{where} 必须是对象")
    tid = _str(spec, "id", where)
    where = f"模板 {tid}"
    if not _ID.match(tid):
        raise CatalogError(f"{where}：id 只能含小写字母、数字和横线")
    if tid == "policy":
        raise CatalogError(f"{where}：id「policy」保留给权限列表申请，请换一个")
    kind = _str(spec, "kind", where)
    if kind not in KINDS:
        raise CatalogError(f"{where}：kind 只能是 {' / '.join(KINDS)}")
    # `_` 开头的是写给人看的说明（为什么这么配、还差什么），不参与校验
    unknown = sorted(k for k in set(spec) - _COMMON - _BY_KIND[kind] if not k.startswith("_"))
    if unknown:
        raise CatalogError(
            f"{where}：{KIND_LABELS[kind]}模板里不认识的字段 {', '.join(unknown)}（拼错了？）"
        )
    platform = _str(spec, "platform", where)
    # 直接读 platforms.IDS，不在这里存一份快照：存了就是第二份真相，
    # 加新平台时只改 platforms.py 就不够了
    if platform not in platforms_mod.IDS:
        raise CatalogError(f"{where}：platform 只能是 {' / '.join(platforms_mod.IDS)}")
    account = _str(spec, "account", where)
    if not _ACCOUNT.match(account):
        raise CatalogError(f"{where}：account 必须是云账号 ID（数字）")
    risk = _str(spec, "risk", where, required=False) or "low"
    if risk not in RISKS:
        raise CatalogError(f"{where}：risk 只能是 {' / '.join(RISKS)}")
    category = _str(spec, "category", where, required=False)
    if len(category) > _CATEGORY_MAX:
        raise CatalogError(f"{where}：category 最长 {_CATEGORY_MAX} 个字")
    groups = spec.get("groups", [])
    if not isinstance(groups, list) or not all(
        isinstance(g, str) and _GROUP.match(g) for g in groups
    ):
        raise CatalogError(f"{where}：groups 必须是用户组名数组")

    kw: dict = {}
    if kind == KIND_PERMISSION:
        if not groups:
            raise CatalogError(f"{where}：权限模板至少要有一个用户组")
        kw["max_days"] = _int(spec, "max_days", where, 0, 0, 3650)
    elif kind == KIND_CREDENTIAL:
        caps = spec.get("caps", [])
        if not isinstance(caps, list) or not caps or not all(c in CAPS for c in caps):
            raise CatalogError(f"{where}：caps 必须是 {' / '.join(CAPS)} 里的非空子集")
        if len(set(caps)) != len(caps):
            raise CatalogError(f"{where}：caps 有重复项")
        allow_prefix = spec.get("allow_prefix", True)
        if not isinstance(allow_prefix, bool):
            raise CatalogError(f"{where}：allow_prefix 必须是 true / false")
        # role_arn 可以留空：这个模板就只发长期凭证（≤12 小时的申请会被挡下并说明原因）。
        # 留空比填一个不存在的角色好——填错要等到有人真的申请、审批通过、开通那一刻才炸。
        role = _str(spec, "role_arn", where, required=False)
        if role:
            match = platforms_mod.get(platform).role_pattern.match(role)
            if not match:
                raise CatalogError(f"{where}：role_arn 格式不对")
            if match.group("account") != account:
                raise CatalogError(f"{where}：role_arn 不属于云账号 {account}")
        kw["role_arn"] = role
        kw["caps"] = tuple(c for c in CAPS if c in caps)
        kw["buckets"] = _buckets(spec, where)
        kw["allow_prefix"] = allow_prefix
        # 会话策略（发凭证时把角色现场收窄到单桶单目录）取证过的平台，≤12 小时走 STS，
        # 所以必须配角色；没取证过的平台一律走长期凭证，不配角色也不许配 ——
        # 不收窄就发等于把整个角色的范围交出去。这个开关在 platforms.py。
        cloud = platforms_mod.get(platform)
        if cloud.session_policy and not role:
            raise CatalogError(
                f"{where}：{cloud.name}凭证模板要配 role_arn（{STS_MAX_HOURS} 小时以内走它换 STS）"
            )
        if not cloud.session_policy and role:
            raise CatalogError(f"{where}：{cloud.name}的凭证一律走长期路径，不要配 role_arn")
        kw["max_hours"] = _int(spec, "max_hours", where, 1, 1, MAX_CREDENTIAL_HOURS)
    elif kind == KIND_RESOURCE:
        kw["max_days"] = _int(spec, "max_days", where, 0, 0, 3650)
        kw["options"] = _options(spec, where)
        kw["params"] = _params(spec.get("params", {}), where)
        kw["cost_centers"] = _cost_centers(spec, where)
        other = spec.get("cost_center_other", False)
        if not isinstance(other, bool):
            raise CatalogError(f"{where}：cost_center_other 必须是 true / false")
        if other and not kw["cost_centers"]:
            raise CatalogError(f"{where}：没有 cost_centers 就不用配 cost_center_other")
        kw["cost_center_other"] = other
        kw["spec_hint"] = _str(spec, "spec_hint", where, required=False)
        if len(kw["spec_hint"]) > SPEC_MAX:
            raise CatalogError(f"{where}：spec_hint 最长 {SPEC_MAX} 个字")
        rtype = _str(spec, "resource_type", where, required=False)
        region = _str(spec, "region", where, required=False)
        if kw["options"]:
            # 配了选项轴 = 要面板自己建。建之前必须知道建什么、建在哪
            if rtype not in RESOURCE_TYPES:
                raise CatalogError(
                    f"{where}：配了 options 就要指定 resource_type（目前支持 "
                    f"{' / '.join(RESOURCE_TYPES)}）"
                )
            if not _REGION_ID.match(region):
                raise CatalogError(f"{where}：配了 options 就要指定 region（例如 cn-hangzhou）")
        elif rtype or region or kw["params"]:
            raise CatalogError(
                f"{where}：没配 options 的资源模板由人工开通，"
                f"不要填 resource_type / region / params"
            )
        kw["resource_type"] = rtype
        kw["region"] = region
    else:
        pattern = _str(spec, "username_pattern", where, required=False) or DEFAULT_USERNAME
        if not pattern.startswith("^") or not pattern.endswith("$"):
            raise CatalogError(f"{where}：username_pattern 必须以 ^ 开头、$ 结尾")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CatalogError(f"{where}：username_pattern 不是合法正则：{exc}") from exc
        console = spec.get("console_login", False)
        if not isinstance(console, bool):
            raise CatalogError(f"{where}：console_login 必须是 true / false")
        kw["username_pattern"] = pattern
        kw["console_login"] = console

    return Template(
        id=tid,
        kind=kind,
        platform=platform,
        account=account,
        title=_str(spec, "title", where),
        description=_str(spec, "description", where, required=False),
        risk=risk,
        category=category,
        groups=tuple(groups),
        **kw,
    )


@dataclass(frozen=True)
class Catalog:
    templates: tuple = ()

    def get(self, template_id: str) -> Optional[Template]:
        return next((t for t in self.templates if t.id == template_id), None)

    def of_kind(self, kind: str) -> tuple:
        return tuple(t for t in self.templates if t.kind == kind)


def parse(data: object) -> Catalog:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CatalogError(f"模板目录 schema 必须是 {SCHEMA}")
    items = data.get("templates")
    if not isinstance(items, list):
        raise CatalogError("模板目录缺 templates 数组")
    templates = [parse_template(spec, i) for i, spec in enumerate(items)]
    ids = [t.id for t in templates]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise CatalogError(f"模板 id 重复：{', '.join(dup)}")
    return Catalog(tuple(templates))


def load(path: Optional[str]) -> Catalog:
    """文件不存在 = 没有可申请的模板（安全的一侧），格式错误则报错。"""
    if not path or not Path(path).exists():
        return Catalog()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"读不了模板目录 {path}：{exc}") from exc
    return parse(data)

# MLflow 网关要改的十几行

`tensorboard.wuji-tech.com`（`39.108.82.208`）上 `feishu_auth.py` + nginx 的改动。
**面板侧全部做完、验过之后才动这里**；在那之前这台机一个字不用改，现状照常。

## 一、`feishu_auth.py` 加一个判定

照着现有 `is_disabled()` 的形状写第三个「问一下再放行」，**但 fail 的方向相反**：

```python
import urllib.request

PANEL = os.environ.get("PANEL_URL", "https://cloud.wuji-tech.com")
PANEL_TOKEN = os.environ.get("PANEL_SERVICE_TOKEN", "")
SERVICE = os.environ.get("PANEL_SERVICE", "mlflow")
_grant_cache: dict = {}  # union_id -> (decision, expires_at)


def panel_allows(union_id: str) -> dict:
    """面板批准过这个人用这个服务吗。

    **查不到就拒**（返回 allowed=False）—— 和上面 `is_disabled()` 查库失败时放行
    正好相反，这是有意的：禁用名单查不到不该误伤正常人；而授权名单查不到就放行的话，
    面板一挂这道门就等于不存在。两个相邻的函数一个 fail-open 一个 fail-closed，
    别顺手统一。

    缓存 60 秒，同 `is_disabled` —— 撤销最迟一分钟生效。
    """
    now = time.time()
    hit = _grant_cache.get(union_id)
    if hit and hit[1] > now:
        return hit[0]
    out = {"allowed": False, "reason": "panel_down", "message": "权限服务暂时不可用，请稍后再试"}
    try:
        req = urllib.request.Request(
            PANEL + "/api/service-access",
            data=json.dumps({"service": SERVICE, "union_id": union_id}).encode(),
            headers={"Authorization": "Bearer " + PANEL_TOKEN,
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            out = {"allowed": False, "reason": "panel_busy",
                   "message": "权限服务繁忙，请稍后再试"}
    except Exception:
        pass  # 保持 fail-closed 的默认值
    # 缓存时长分三档，**这个分档是有意的**：
    #   · 放行 60 秒 —— 撤销最迟一分钟生效
    #   · 面板明确说「没权限」10 秒 —— 人刚申请下来不该等一分钟
    #   · **面板出错 / 限流 60 秒** —— 这一档不能跟着「没权限」走 10 秒：
    #     面板一旦回 429，短缓存会让每个人的重问频率翻 6 倍 → 限流窗口永远清不空 →
    #     全员常闭且不会自愈。出错时退避，才不会自己把自己打死
    failed = out.get("reason") in ("panel_down", "panel_busy")
    ttl = 60 if (out.get("allowed") or failed) else 10
    _grant_cache[union_id] = (out, now + ttl)
    return out
```

挂进 `/oauth2/auth`，**排在禁用名单之后、认领之前**：

```python
@app.get("/oauth2/auth")
def auth(request: Request):
    o = unpack(request.cookies.get(COOKIE, ""))
    if not o:
        return Response(status_code=401)
    if is_disabled(o["u"]):
        return Response(status_code=401)
    # 两条拒绝都是 403，靠响应头区分该把人送去哪（见下一节）
    if AUTH_GATE and not panel_allows(o["u"]).get("allowed"):
        return Response(status_code=403, headers={"X-Panel-Reason": "no_grant"})
    if AUTH_GATE and not request.cookies.get("tb_skip") and not has_claimed(o["u"]):
        return Response(status_code=403, headers={"X-Panel-Reason": "unclaimed"})
    ...
```

## 二、nginx：403 分流

现在 `/` 的 `error_page 403` 一律转 `/claim`。「没授权」和「未认领」都是 403，
但该把人送去的地方不同 —— 没授权要去面板提单，未认领要去认领页。
送错了人就卡死在一个对他没用的页面上，而页面本身不会有任何错误提示。

**不能用不同的状态码分。** `auth_request` 只认三种结果：2xx 放行、401 和 403 拒绝，
**返回任何其它码都被当成错误 → 502/500**。所以「没授权 403、未认领 402」这种写法
会让未认领的人看到一个 5xx 错误页。（上线时顺手实测一次：`curl -i` 打一个未认领的
cookie，看回的是不是 302 到认领页，而不是 5xx。）

正确的分法是**同一个 403，带响应头**（上一节的钩子里已经带上了），
nginx 用 `auth_request_set` 把它取出来分流：

```nginx
location / {
    auth_request /oauth2/auth;
    auth_request_set $panel_reason $upstream_http_x_panel_reason;
    error_page 401 =302 https://$host/welcome?rd=$scheme://$host$request_uri;
    error_page 403 = @denied;
    ...
}

location @denied {
    # 没授权 → 面板的申请页，直接落到这条模板上
    if ($panel_reason = "no_grant") {
        return 302 https://cloud.wuji-tech.com/#apply=mlflow-access;
    }
    # 其余（未认领，以及头丢了的情况）→ 保持原来的认领页
    return 302 https://$host/claim;
}
```

**默认那条要留给认领页**，不是申请页：头没取到时（网关旧版本、中间有代理把头剥了）
走的是这条。未认领的人被送去申请页，他会提一张单、批下来、发现还是进不去 ——
而反过来（没授权的人被送去认领页）他至少能看出自己没有权限。两种错都会发生，
选表现得更明显的那种。

## 三、环境变量（`/etc/feishu-auth/env`）

```
PANEL_URL=https://cloud.wuji-tech.com
PANEL_SERVICE=mlflow
PANEL_SERVICE_TOKEN=<面板那边生成的令牌>
```

令牌由面板侧的 `DELIVERY_SERVICE_TOKENS_FILE` 配出来，形如
`{"mlflow": {"tokens": ["新", "旧"]}}` —— **tokens 是数组**，轮换时新旧并存，
换完删掉旧的，不用重启任何一边。

## 四、上线顺序和回滚

1. 先把 `panel_allows` 加上但**不挂进 `/oauth2/auth`**，用 `curl` 在那台机上直接调一次面板，确认通；
2. 再挂进去，**先拿一个已授权的人试**（面板上给自己提一张单走完）；
3. 再拿一个没授权的人试，确认被转到申请页；
4. 回滚就是把那三行 `if AUTH_GATE and not panel_allows(...)` 注释掉、重启 `feishu-auth`。

## 五、已知限制

- **面板挂了超过 60 秒，所有人都进不去**，不只是新用户 —— 缓存只有 60 秒。
  这包括**面板每次部署重启的那几秒到几十秒**：上线后第一次部署面板时，
  会有人在那个窗口里进不去 MLflow，别当成 MLflow 挂了。这是 fail-closed 的代价，
  换来的是「面板说不行就真的不行」。
- **`/api/` 那条 location 完全绕过这套**：它走 `auth_basic` + `/etc/nginx/.tb_htpasswd`，
  拿到那份账密的人能读写删所有实验，不受授权和禁用名单约束、离职了也照样能用。
  SDK 上报要用所以这次不动，但门做完之后它就是这套门禁唯一的缺口。
- **MLflow 里没有按人隔离**（`AUTH_PLUGIN=0`）：过了门的人能看到、改、删所有人的实验。
  申请页的说明里写了这一点，但这是产品事实，不是门禁能解决的。

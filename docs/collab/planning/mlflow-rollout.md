# MLflow 接入上线清单

顺序不能换。每步都有「怎么确认它真的生效了」，不确认不往下走。

## 0. 前置确认（只读）

```bash
ssh panel 'systemctl is-active cloud-panel; ls -l /opt/infra/identity/approval.json'
ssh panel 'cd /opt/infra && sudo -u delivery python3 deploy/panel/preflight.py --env /etc/delivery/panel.env; echo rc=$?'
```
`rc != 0` 就别继续。**只喂 panel.env**，别喂 refresh.env。

## 1. 部署面板代码

```bash
rsync -a --chown=delivery:delivery src/ panel:/opt/infra/src/
ssh panel 'systemctl restart cloud-panel && sleep 2 && systemctl is-active cloud-panel'
```

**只同步 `src/`。`identity/` 一个字节都不能碰**（那是活数据，本地那份内容不一样）。

确认：
```bash
ssh panel 'curl -s http://127.0.0.1:8765/requests.js | grep -c "内部服务"'   # 应该 > 0
ssh panel 'ls -l /opt/infra/src/delivery/service_access.py'                  # 属主必须是 delivery
```

## 1.5 面板 nginx 放行 `/api/service-access`（**`deploy/` 不会被同步，必须手工改线上**）

网关跑在自己的公网机器上，不在 `$office` 网段里 —— 不加这条 location，请求会落进
`location /` 的网段门、拿到 **nginx 的 403**。而**面板的令牌门也是 403**，光看状态码
分不出来；偏偏第 4 步刚生成完令牌，看到 403 的第一反应必然是「令牌写错了」，
方向一开始就错。

照 `deploy/panel/nginx-cloud-panel-common.conf` 里那条，加进线上的「对外开放」段
（第 1 步只同步 `src/`，这个文件是模板，线上那份 nginx 配置是手工维护的）：

```nginx
location = /api/service-access { proxy_pass http://127.0.0.1:8765; }
```

```bash
ssh panel 'nginx -t && nginx -s reload'
```

线上若走 oauth2-proxy：同时加 `--skip-auth-route='^/api/service-access$'` 并重启它。
**那是命令行参数，不是 alpha 配置文件里的字段。**

确认 —— **必须看响应体，不能只看状态码**：

```bash
curl -i -sS https://cloud.wuji-tech.com/api/service-access \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' \
  -d '{"service":"mlflow","union_id":"x"}'
```

| 看到什么 | 说明 |
|---|---|
| `{"error":"unauthorized"}` | 请求到了面板，这步 OK（令牌是故意写错的） |
| `not in company network` | 这步没生效，还在 nginx 那儿被挡着 |

## 2. 装模板 + 审批定义（**必须在第 1 步之后**）

顺序反了 = 旧代码读到 `kind: service` 的模板 → `catalog` 硬拒未知 kind →
`/api/requests/options` 502 → **整个申请页白屏**。本地实测过。

```bash
scp install_mlflow.py panel:/tmp/
ssh panel 'cd /opt/infra && sudo -u delivery python3 /tmp/install_mlflow.py'          # 预演
ssh panel 'cd /opt/infra && sudo -u delivery python3 /tmp/install_mlflow.py --apply'
```

**必须 `sudo -u delivery`**。用 root 写 identity/ 会把属主改成 root，面板从此读不了
那个文件，而且**没有任何告警**（2026-09-22 踩过，挂了 1 小时 40 分）。

**改完 approval.json 要 `systemctl restart cloud-panel`，不是 reload。**
审批回调的 code 白名单（`Backend.approval_hook`）是**进程级建一次**缓存到结束的，
不重启的话新定义的回调照样被丢 —— 表现和"没订阅"一模一样，会白查半天。

确认：
```bash
ssh panel 'find /opt/infra/identity -user root'        # 必须为空
ssh panel 'systemctl restart cloud-panel'
ssh panel 'curl -s http://127.0.0.1:8765/api/requests/options -H ... | grep mlflow'
```

## 3. 订阅新审批定义（**最容易漏的一条**）

面板靠飞书事件订阅收审批回调，而**每条定义都要单独订阅**。不订阅的表现是：
审批在飞书里批了，面板一条事件都收不到，单子永远停在「审批中」——
两边各自看都正常（飞书说已通过，面板说在等审批）。

```
POST /open-apis/approval/v4/approvals/98039779-906B-493F-946A-B96E685FB640/subscribe
```

重复调返回 `1390007 subscription existed`，所以这个调用**自己就是探针**：
回 `existed` 说明之前就订过了。

确认：真提一张单，在飞书里批掉，看面板上的状态有没有自己变。
**只有这一步能证明整条链通了**，前面所有检查加起来都不能。

### 提那张单时顺便盯「申请类型」那一栏

`approval.create()` 无条件把 `kind`（这里是字符串 `service`）塞进必填的「申请类型」
控件，而且它**没有** extra 那条「定义里没有就跳过」的保护。如果新定义里这一栏是
**单选**、选项 key 又不是 `service`，飞书拒的是**整张表单** → 单子直接落「提交失败」。

`delivery approval widgets` 只查控件 id，查不出选项 key，所以这个错**配置期发现不了**。

- 最省事：把那一栏建成**文本控件**（服务访问只有一种类型，单选没有意义）；
- 或者单选里精确放一个 key 为 `service` 的选项。

提单时看审批单上「申请类型」那栏有没有渲染出来 —— 空白就是这个问题。

## 4. 生成网关令牌

在**面板服务器上**生成、直接写进令牌文件，再从服务器推到网关那台。
**全程不打印到终端/对话里。**

**别手写嵌套引号**（本地 shell → ssh → sh -c → python -c 是四层，这个项目在 SSH
迁移链上已经栽过两次）。这里引号被吃掉的后果特别糟：文件成了空的或非法 JSON →
`_service_tokens()` 返 `{}` → **该服务全员 403，而且没有任何日志**。用 heredoc：

```bash
ssh panel "sudo -u delivery python3 - <<'EOF'
import json, os, secrets, tempfile, pathlib
dst = pathlib.Path('/opt/infra/identity/service-tokens.json')
fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
os.write(fd, json.dumps({'mlflow': {'tokens': [secrets.token_urlsafe(32)]}}).encode())
os.close(fd); os.chmod(tmp, 0o600); os.replace(tmp, dst)
print('written', dst)
EOF"
```

**临时文件 + `os.replace`，不要就地 `>` 截断重写**：`_service_tokens()` 读到半截文件
会返 `{}`（它不回落上一份好缓存），那一瞬间该服务全员被拒。

`DELIVERY_SERVICE_TOKENS_FILE=/opt/infra/identity/service-tokens.json` 写进 panel.env，
**然后 force-recreate / 重启**（改 .env 后 restart 不一定重载，见 deploy_env_reload_gotcha）。

注意令牌文件的格式：`{"mlflow": {"tokens": ["..."]}}` —— **tokens 是数组**。
写成字符串的话整条 spec 会被作废（刚修的那条保护），表现是该服务全员 403。

### 以后怎么轮换

`tokens` 是数组的唯一理由就是轮换。顺序是**新旧并存 → 换网关 → 删旧的**，
中间不用重启任何一边（面板按 mtime 重读）：

1. 往数组里**加**一条新令牌（旧的留着），原子写；
2. 把网关的 `PANEL_SERVICE_TOKEN` 换成新的，重启 `feishu-auth`；
3. 确认网关能查通之后，再把旧的那条从数组里删掉。

先删旧的再换网关 = 中间那段时间全员进不去。

## 5. 网关补丁（`39.108.82.208`）

照 `docs/collab/research/mlflow-gateway-patch.md`：
1. 先只加 `panel_allows`，**不挂进 `/oauth2/auth`**，在那台机上 `curl` 面板确认通；
2. 挂进去，先拿一个**已授权**的人试；
3. 再拿一个**没授权**的人试，确认被转到申请页；
4. nginx 的 403 分流（响应头 `X-Panel-Reason`，不能用不同状态码 —— `auth_request`
   只认 2xx/401/403，别的码会变成 5xx）。上线时实测一次未认领的 cookie，
   确认回的是 302 到认领页而不是 5xx。

回滚：把那三行 `if AUTH_GATE and not panel_allows(...)` 注释掉、重启 `feishu-auth`。

## 拦截开关（`PANEL_GATE`）—— **现在刻意关着，别顺手打开**

代码和 nginx 分流都已就位，但 `/etc/feishu-auth/env` 里**没有** `PANEL_GATE=1`，
所以网关行为和打补丁前一模一样。

**为什么不开**（2026-09-23 决定）：
1. **现存用户还没认领完。** 开了之后没有授权单的人全部被转到申请页 ——
   而认领流程本身还在进行中，等于同时推两件事。
2. 开的那一刻起，「有没有授权单」就是能不能进的唯一判据，而模板刚上线，单子数为 0。

开的条件：认领完成 + 至少跑通一张真单（提单 → 飞书批 → 面板变「已开通」→
网关查到 `allowed: true`）。开法是往那个 env 加一行 `PANEL_GATE=1` 并
`systemctl restart feishu-auth`；关回去同理，不用动代码。

## 已知代价（上线后会发生，别当故障）

- **面板每次部署重启的那几十秒，MLflow 全员进不去**（网关 fail-closed + 60 秒缓存）。
- **名册文件读不了 → 全员进不去**（这是有意的，理由见 `Backend.service_access`）。
- `/api/` 那条 location 走 htpasswd，完全绕过这套门禁 —— SDK 上报要用，这次不动。

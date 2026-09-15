# 云权限面板：公司 IAM 登录

```
浏览器 ──HTTPS──> nginx / SLB ──> oauth2-proxy (127.0.0.1:4180) ──> 面板 (127.0.0.1:8765)
                                   公司 IAM OIDC 登录                 delivery serve --auth proxy
```

面板不处理 OIDC，只认代理注入的请求头；代理和面板之间用共享密钥确认请求确实来自代理。

## 需要 IT 提供

`cloud-panel` OIDC 应用（交接文档第 6 节）：Issuer、client_id、client_secret。
Redirect URI 填代理地址：`https://<面板域名>/oauth2/callback`。

另外要 IT 书面确认两件事：

1. **访问策略**：`cloud-panel` 绑定了「全体在职员工」。代理配置是 `--email-domain='*'`，
   不绑定策略时所有 IAM 账号（含服务账号、外部账号）都能登录面板。
2. **邮箱来源**：IAM 用户的 `email` 从飞书同步、用户自己改不了，userinfo 返回
   `email_verified: true`。如果用户能改邮箱，不要配 `DELIVERY_IAM_USERINFO_URL`，
   改为先用 `delivery identity people --directory feishu` 回填全员 union_id。
   面板要求 `email_verified` 为 true，但 IAM 通常对所有邮箱都返回 true，这一项本身证明不了什么，
   关键是用户不能改邮箱。

## 切换前检查 union_id 一致

面板现有的管理员名单、登录绑定、名册里的 union_id 都来自面板自己的飞书应用；
IAM 给的 `feishu_union_id` 来自 IAM 的飞书应用。两个应用属于同一企业时 union_id 相同，
否则切换后管理员会失去权限、已绑定的人全部冲突。

切换前拿同一个人核对一次：飞书模式登录面板看 `/api/session` 的 `union_id`，
和 IAM 里该用户的 `feishu_union_id` 属性比对，一致再切。

## 启动

```bash
# 1. 共享密钥，代理和面板用同一个值
python -c "import secrets; print(secrets.token_urlsafe(32))" > proxy_secret && chmod 600 proxy_secret

# 2. 面板：只监听本机，由代理转发
DELIVERY_AUTH=proxy \
DELIVERY_PROXY_SECRET_FILE=proxy_secret \
DELIVERY_IAM_USERINFO_URL=https://<IAM 域名>/application/o/userinfo/ \
DELIVERY_IAM_EMAIL_DOMAINS=wuji.tech \
DELIVERY_LOGOUT_URL='/oauth2/sign_out?rd=<URL 编码的 IAM 退出地址>' \
delivery serve --port 8765

# 3. oauth2-proxy：参数见 oauth2-proxy.example.yaml 顶部注释
PANEL_PROXY_SECRET="$(cat proxy_secret)" oauth2-proxy --alpha-config=oauth2-proxy.yaml ...
```

| 环境变量 | 说明 |
|---|---|
| `DELIVERY_AUTH` | `proxy`；不设或 `feishu` 为原来的飞书登录 |
| `DELIVERY_PROXY_SECRET` / `_FILE` | 共享密钥，至少 32 个 ASCII 字符，二选一 |
| `DELIVERY_IAM_USERINFO_URL` | 可选。名册里还没有某人 union_id 时，查 IAM 邮箱做首次关联 |
| `DELIVERY_IAM_EMAIL_DOMAINS` | 配了 userinfo 就必填，逗号分隔。只接受这些域名、且 `email_verified` 为 true 的邮箱 |
| `DELIVERY_LOGOUT_URL` | 可选，默认 `/oauth2/sign_out`（只清代理会话，不退出 IAM） |

## 安全要点

- 面板和代理端口都只监听本机，HTTPS 终止在前面的 nginx / SLB。密钥对不上的请求一律按未登录处理。
- 代理 `emailClaim` 指向 `feishu_union_id`：IAM 没返回 union_id 的用户建不了会话；没有邮箱的员工照常登录。
- 客户端伪造 `X-Panel-*` 请求头无效，代理会删掉客户端带的同名头再注入。
- 代理模式下管理员只按 `admins.json` 的 `union_ids` 判断，邮箱条目不生效。
- IAM 邮箱只用于名册首次关联，不用于判断权限。查 userinfo 不跟随重定向，避免 token 被带到别的地址。
- 会话最长 8 小时（`--cookie-expire=8h`）。IAM 停用某人后，他已有的会话最多还能用到过期。
- 代理模式下面板的 `/auth/*`（飞书登录、CLI 换票）全部关闭。CLI 走 IAM 另做。

## 回退

去掉 `DELIVERY_AUTH=proxy`，重新配飞书应用环境变量，访问面板端口即可。

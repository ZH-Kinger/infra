# 只能 SSH 登录的 GPU 集群纳入公司 IAM（Authentik）—— 方案调研

调研人：researcher　日期：2026-09-16
范围：九章（北京 B200，`core/jiuzhang_transfer`）、新加坡中转 + 泰国 H200（`core/ssh_transfer`）
方法：只读。代码结论来自 `/home/l/桌面/langchaindev` 工作区本地文件；外部结论来自官方文档与上游源码，均附 URL。**本次没有 SSH 连接任何服务器，没有访问 `iam.wuji-tech.com`，没有调用任何 IAM 接口，没有改动任何源码。**

可信度标记：`【代码】`读本仓库源码得出，附文件:行号；`【文档】`官方文档明说并附原文；`【实测】`本机可复现的验证；`【推断】`由前述材料推导、未逐字写明；`【拿不准】`需另行取证。

---

## 1. 现状核实

### 1.1 三台机器的连接方式

| 机器 | 地址 | 登录用户 | 私钥来源 | host key 校验 |
|---|---|---|---|---|
| 九章（北京 B200） | `221.199.124.79:30019` | `root` | `JIUZHANG_SSH_KEY_ENC`，留空回落 `SGP_SSH_KEY_ENC` | `JIUZHANG_HOST_KEY` 固定 + `RejectPolicy` |
| SGP 中转 | `43.98.203.59:22` | `root` | `SGP_SSH_KEY_ENC` | `SGP_SSH_HOST_KEY` 固定 + `RejectPolicy` |
| 泰国 H200 | `203.156.3.194:40002` | `wuji` | 不由 bot 直连；走 SGP→泰国 第二跳，用 SGP 上已配好的免密 | 由 SGP 侧 `known_hosts` 负责 |

出处【代码】：`config/settings.py:382-386`（SGP）、`config/settings.py:394-396`（泰国）、`config/settings.py:411-418`（九章）；`core/jiuzhang_transfer/engine.py:81-84`（`username=getattr(settings, "JIUZHANG_USER", "root") or "root"`，且 `allow_agent=False, look_for_keys=False`）。

### 1.2 一把共享私钥的事实链

`core/jiuzhang_transfer/engine.py:33-39` 的取钥逻辑与注释：

```
"""Fernet 密文 → paramiko key（**只在内存**）。默认复用 SGP 那把（同一把 key 已在九章
的 authorized_keys2 里），配 JIUZHANG_SSH_KEY_ENC 可单独换。"""
enc = (getattr(settings, "JIUZHANG_SSH_KEY_ENC", "") or settings.SGP_SSH_KEY_ENC or "").strip()
```

即：默认情况下 bot 用**同一把私钥**登录 SGP 和九章，两台机器都以 `root` 身份进入。私钥以 Fernet 密文存在配置里，运行时解密进内存、不落盘（`engine.py:41-49` 校验解密结果含 `-----BEGIN`，否则报错）。host key 固定 + `RejectPolicy`，禁 `AutoAdd`（`engine.py:76-79`）。【代码】

这把钥匙没有任何人的身份信息：它不属于某个人，也不区分是哪个人通过 bot 触发了任务。

九章侧把公钥放在 `authorized_keys2` 而不是 `authorized_keys`，这通常意味着 `authorized_keys` 已被机器的管理方占用、我方的公钥是被追加到第二个文件里的（OpenSSH 的 `AuthorizedKeysFile` 默认值就是 `.ssh/authorized_keys .ssh/authorized_keys2`，两个文件都会被读）。这条是判断「机器归谁管」的一个线索，但不是证据。【推断】

### 1.3 bot 侧已有的归属记录

任务记录里已经有 `created_by`，写的是飞书 `open_id`：

- `core/jiuzhang_transfer/orchestrator.py:72,86`（`create_job_record(plan, *, open_id="")` → `"created_by": open_id`）
- `core/ssh_transfer/orchestrator.py:80,105`，并且 `:87-88` 会在幂等复用旧记录时回填缺失的 `created_by`
- 飞书卡片入口都传了 `open_id`：`core/feishu_bot/actions.py:1472`（九章）、`:1233`（泰国链）

缺口：CLI 入口不传 `open_id`，从命令行起的任务 `created_by` 是空串 —— `core/ssh_transfer/cli.py:40`（`o.create_job_record(plan, bytes_total=b, ...)`，没有 `open_id=`）。九章目前没有 CLI 入口（目录下无 `cli.py`）。【代码】

所以「谁触发了哪个任务」在 bot 侧基本有账；「谁登上了那台机器」在机器侧完全没账 —— 机器只看到某个 IP 以 root 用一把公钥登录。

---

## 2. 不改造的风险

按「造成损失的难易程度」排，不按理论严重性排。

1. **私钥泄露面 = 所有能读到配置的人和进程。** 密文本身在 `.env`（服务器 `/root/langchaindev/.env`），解密密钥是 `BOT_CREDS_ENCRYPTION_KEY`，两者在同一台机器上、同一个 docker-compose 里。拿到服务器 root 就同时拿到两者 → 拿到九章和 SGP 的 root。项目记忆里已记录「服务器 root/Redis + Fernet key 待轮换」的历史事件，说明这条不是假设风险。
2. **人走了没有开关可关。** 现在唯一的失效手段是去三台机器上删公钥、生成新私钥、重新 Fernet 加密、改 `.env`、force-recreate 容器（`docker compose restart` 不重载 `env_file`）。九章那台还要对方配合改 `authorized_keys2`。实际结果是没人会为一次离职做这套动作。
3. **审计分不出人。** 九章的 `/var/log/secure`、`last`、`who` 里只有 root 从 bot 的公网 IP 登录。出了事（删了目录、跑满盘、动了别人的数据）只能定位到「bot 干的」，再回 Redis 里翻 `created_by` 才能推到人；而 CLI 起的任务连这一步都断了（§1.3）。对方如果来问责，我方没有机器侧证据。
4. **权限是 root，不是最小权限。** 迁移链只需要读写一个目的目录（`JIUZHANG_DEST_ROOT=/root/nas`）+ 跑 `ossutil`。现在给的是整机 root。任何一次命令拼接失误（虽然 `core/ssh_transfer/paths.py` 有白名单）都是整机级后果。
5. **一把钥匙串三台机器。** `JIUZHANG_SSH_KEY_ENC` 默认回落 `SGP_SSH_KEY_ENC`，SGP 上又存着到泰国的免密。SGP 被拿下 = 三台全下。爆炸半径没有被分段。

这些风险里，2 和 3 正是「纳入 IAM」能直接解决的；1 和 5 靠短期证书（不再有长期私钥）顺带解决；4 要靠在机器上建专用账号，跟身份方案是两件事。

---

## 3. Authentik 能给 SSH 提供什么

先说一个事实：**authentik 本身不是 SSH CA，也没有签发 SSH 证书的能力。** 官方 provider 列表里是 OAuth2/OIDC、SAML、LDAP、RADIUS、Proxy、SCIM、RAC、Google Workspace/Entra 同步，没有 SSH 证书签发。相关 issue 是「让 Docker SSH Outpost 用短期 SSH 证书」（goauthentik/authentik#2916）和「允许在用户 Profile 里加 SSH Key」（#2145），都不是「authentik 当 CA 给人签证书」。所以 SSH CA 这条路必然是「authentik 做 OIDC 身份源 + 另一个组件做 CA」。【文档/推断】
- https://goauthentik.io/features/
- https://github.com/goauthentik/authentik/issues/2916
- https://github.com/goauthentik/authentik/issues/2145

下面五条路，按「需要对方配合的程度」从低到高排。

### 3.1 路 A：RAC（authentik 自带的浏览器堡垒机）—— 机器零改动

authentik 的 Remote Access Control provider 基于 Apache Guacamole，支持 RDP/SSH/VNC：「The RAC provider allows users to access remote Windows, macOS, and Linux machines via RDP/SSH/VNC.」
- https://docs.goauthentik.io/add-secure-apps/providers/rac/

要素：
- 部署一个 **RAC Outpost**（Docker/K8s，部署在我方）：「The RAC provider requires the deployment of an RAC Outpost.」
- 每台目标机建一个 **Endpoint**：「Endpoints define the IP address, port, protocol, and other settings used for connecting to a remote machine.」
- 凭证走 **Property Mapping**：「RAC property mappings can be used to pass the access credentials and connection settings of the remote machine.」支持 Guacamole 的 `username` / `password` / `private-key` 等参数。
- 会话随 authentik session 结束而断。
- https://docs.goauthentik.io/add-secure-apps/providers/rac/how-to-rac/
- 公钥认证有专页：https://docs.goauthentik.io/add-secure-apps/providers/rac/rac-public-key/

**2025.2 起 RAC 从企业版转成开源免费**（「Remote Access Control ... is now free and open source」），IT 侧的 2025.6.4 已经在这之后，不需要企业授权。
- https://goauthentik.io/blog/2025-02-04-open-source-rac-and-pricing-support-updates/

对本场景的意义：
- 目标机**一行都不用改**，网络方向也和现在一样（我方 outpost 主动连 `九章:30019`），不需要机器能访问 IAM。
- 得到的是：谁能打开这台机器的终端由 authentik 的 Application 授权决定，人走了在 authentik 里禁用即刻失效；authentik 侧有事件日志记录谁在什么时候打开了哪个 endpoint。
- 得不到的是：机器侧仍然是同一个 root 账号，`/var/log/secure` 里还是分不出人；而且这条路**只解决人工登录，解决不了 bot 的程序化 SSH**（bot 的 paramiko 不可能走浏览器 Guacamole）。
- 会话录像：Guacamole 引擎本身支持录屏/录终端，但 authentik 是否把这个参数暴露出来、能不能落盘，文档里没写，且已知 property mapping 传 Guacamole 连接参数有 bug 报告（#15783）。【拿不准】
  - https://github.com/goauthentik/authentik/issues/15783

运维成本：一个 outpost 容器 + 每台机一条 endpoint 配置。低。

### 3.2 路 B：SSH CA（机器只加一行 `TrustedUserCAKeys`）

模型：人先用 OIDC 登 authentik，换一张有效期几小时的 SSH 用户证书；目标机的 sshd 只信任 CA 公钥，不再存任何人的公钥。

sshd 侧只需要（OpenSSH 原生能力，无需装任何 daemon）：
- `TrustedUserCAKeys` —— 「Specifies a file containing public keys of certificate authorities that are trusted to sign user certificates for authentication, or `none` to not use one.」
- 可选 `AuthorizedPrincipalsFile` —— 「Specifies a file that lists principal names that are accepted for certificate authentication.」
- 可选 `RevokedKeys` —— 「Specifies revoked public keys file...Keys listed in this file will be refused for public key authentication.」
- https://man.openbsd.org/sshd_config

**机器不需要能访问 IAM。** 验证证书只用本地那份 CA 公钥，登录时刻不产生任何对外网络请求。这是这条路相对 LDAP/SSSD 的决定性优势，尤其是在「国内机房 + 我方 IAM 在哪不确定」的前提下。【推断，依据是 sshd 的验证逻辑纯本地】

审计：sshd 会把证书的 ID 和 serial 写进日志，形如
`Accepted publickey for developer from ... ED25519-CERT ID e662bf1e-... (serial 0) CA ED25519 SHA256:...`
配合 CA 侧的签发日志就能定位到具体的人。
- https://keybase-ssh-ca-bot.readthedocs.io/en/latest/sshca.html

失效：证书到期即失效，不需要碰机器。人走了在 authentik 里禁用 → 换不到新证书 → 最长一个证书有效期后自然断（step-ca 默认 16 小时）。撤销名单（KRL/`RevokedKeys`）只在需要「立刻切断已签发证书」时才用得上。

CA 由谁来做，三个现实选项：

**B1. smallstep step-ca + OIDC provisioner**
OIDC provisioner 需要 `clientID` / `clientSecret` / `configurationEndpoint`（IdP 的 discovery 地址）/ `admins` / 可选 `domains`；它能签发 X.509 和 SSH 用户证书，用户执行 `step ssh login` 走浏览器 SSO，短期证书直接进 ssh-agent。
- https://smallstep.com/docs/step-ca/provisioners/
- https://smallstep.com/docs/step-cli/reference/ssh/certificate/
- 服务器侧配置示例（`TrustedUserCAKeys` / `HostKey` / `HostCertificate`）：https://smallstep.com/docs/tutorials/ssh-certificate-login/

注意区分：smallstep 的**商业 SSH 产品**在主机上要装 agent（做 NSS/JIT 账号、PAM 审计），开源 step-ca 只做签名、主机侧就是 sshd 那几行。别拿商业版文档去评估开源版的落地成本。
- https://smallstep.com/docs/ssh/how-it-works/

是否有人用 authentik 当 OIDC 源接 step-ca：没找到官方文档；能找到的同类实践是 Keycloak 的。由于 OIDC provisioner 只要求标准 discovery + client credentials，authentik 作为通用 OIDC provider 理论上等价可用。【推断】【拿不准：需要实际对接一次验证 authentik 的 discovery/claims 是否满足 step-ca 的 `getIdentity` 逻辑】
- 同类实践（Keycloak）：https://www.maksonlee.com/enable-step-ca-ssh-certificates-with-keycloak-oidc-groups-%E2%86%92-principals-configure-ubuntu-servers-to-trust-the-ca-ubuntu-24-04-windows-11/

**B2. HashiCorp Vault SSH Secrets Engine（signed SSH certificates）**
Vault 挂 ssh 引擎当 CA，用户用 OIDC 登 Vault 后把自己的公钥送去签名；主机侧同样只配 `TrustedUserCAKeys /etc/ssh/trusted-user-ca-keys.pem`，CA 公钥通过免鉴权 API 取。
- https://developer.hashicorp.com/vault/docs/secrets/ssh/signed-ssh-certificates
- https://developer.hashicorp.com/vault/docs/secrets/ssh
成本：需要额外维护一套 Vault（含 unseal / 备份 / 升级）。如果公司没有现成 Vault，为了 SSH 引一套 Vault 不划算。

**B3. Teleport**
Teleport 内置 SSH CA 和会话录制，但 **OIDC / SAML 连接器是企业版功能**，社区版只有 GitHub connector：「The Team and Community versions of Teleport do not support the use of SAML or OIDC - just the Enterprise versions.」
- https://goteleport.com/docs/zero-trust-access/sso/integrate-idp/
- https://goteleport.com/blog/community-github-saml-sso/

结论：**在不买 Teleport Enterprise 的前提下，Teleport 接不上 Authentik。** 这条不作为候选，除非后面确实要买。

### 3.3 路 C：opkssh（OIDC 直接当 SSH 凭证，明确支持 Authentik）

Cloudflare/BastionZero 开源的 opkssh：`opkssh login` 走浏览器 SSO，把 OIDC ID Token 塞进 SSH 证书的扩展字段，sshd 通过 `AuthorizedKeysCommand` 调 `opkssh verify` 校验。
- https://github.com/openpubkey/opkssh
- https://blog.cloudflare.com/open-sourcing-openpubkey-ssh-opkssh-integrating-single-sign-on-with-ssh/

服务器侧要做的事（比路 B 多）：
- 以 root 跑安装脚本，装 `/usr/local/bin/opkssh` 二进制，建低权限系统用户 `opksshuser`
- 改 `/etc/ssh/sshd_config`：
  `AuthorizedKeysCommand /usr/local/bin/opkssh verify %u %k %t` + `AuthorizedKeysCommandUser opksshuser`
- 维护 `/etc/opk/providers`（允许的 OIDC issuer + client id + 过期策略）和 `/etc/opk/auth_id`（哪个身份能登哪个本地账号），权限 640/600
- **服务器需要出站访问 IdP**（校验 ID Token 要拉 JWKS）

Authentik 在「已验证可用的自定义 provider」清单里（Authelia, **Authentik**, AWS Cognito, Entra ID, GitLab, Kanidm, Keycloak, PocketID, Zitadel）。默认公钥有效期 24 小时，可选 12h/48h/1week 或跟随 ID Token。

评价：功能上等价于路 B，但**把「机器不需要访问 IAM」这个优势丢了**，还多了一个要装二进制、建系统用户、改 `AuthorizedKeysCommand` 的动作。对「不是我们自己的机器」这个前提不友好。适合我们完全掌控的机器（比如 SGP 中转）。

### 3.4 路 D：LDAP Outpost + SSSD

authentik 起一个 LDAP provider + LDAP Outpost，目标机装 `sssd` 把 authentik 当 LDAP 用，人在机器上有真实的 Unix 账号。
- https://docs.goauthentik.io/add-secure-apps/providers/ldap/
- https://integrations.goauthentik.io/infrastructure/sssd/

机器侧要做的：装 `sssd` 包、写 `/etc/sssd/sssd.conf`、用 `authconfig` 或 `pam-auth-update` 把系统切到 sssd、`systemctl restart sssd`、还要自己解决 home 目录自动创建。官方原文：「This guide helps you configure `sssd.conf` for LDAP only. You likely need to perform other tasks for a usable setup, such as setting up auto-mounted or auto-created home directories.」

网络：`ldap_uri = ldaps://${authentik.company}:636`，**机器必须能持续访问 LDAP outpost 的 636 端口**。缓解办法是 outpost 不一定要放在 IAM 旁边 —— outpost 是主动向 authentik core 建连的组件，可以部署在离九章网络更近的地方（例如阿里云杭州），让九章只需要访问那个 outpost。【推断】

已知限制（官方原文）：
- 「authentik supports _only_ user and group objects. As a consequence, it cannot be used to provide automount or sudo configuration, nor can it provide netgroups or services to `nss`. Kerberos is also not supported.」—— sudo 规则不能集中下发，还得在机器上另配。
- 「authentik does not define a loginShell attribute by default. Users without an explicit shell setting will be assigned the following default shell: `/bin/sh`」
- MFA：只支持 DUO/TOTP/static，且 SMS 不行（bind 期间没法回发短信）。
- 缓存 bind 模式下「revoking sessions does not remove them from the outpost」。

**离职失效这一条有个坑，必须知道**（官方原文）：
> 「Please note that by default, sssd returns all user accounts; active and disabled. This means that disabled user accounts can still authenticate via `sshPublicKey`. To prevent this, you can filter out disabled user accounts by adding the following lines to the LDAP section of your `sssd.conf` file:」
> ```ini
> ldap_access_order = filter
> ldap_access_filter = ak-active=true
> ```
也就是说，照抄文档的默认配置，**在 authentik 里禁用一个人，他仍然能用 sshPublicKey 登进去**。必须显式加这两行。这正好是我们这次要解决的问题本身，所以它是本方案的必查项而不是可选项。

SSH 公钥怎么进来：「You can store SSH authorized keys in LDAP by adding the `sshPublicKey` attribute to any user with their public key as the value.」—— 即在 authentik 用户的 `attributes` 里写 `sshPublicKey`。这条和已有调研 `authentik-attribute-write-api.md` 里的属性写入接口是同一套机制，可以复用那份接口申请。

评价：能力最完整（真实 Unix 账号、真实 `last`/`wtmp` 记录、可做每人一个 home），改动也最大，且依赖机器能长期访问我方 LDAP 端点。**对「机器不是我们的」场景不现实。**

### 3.5 路 E：RADIUS Outpost

authentik 有 RADIUS provider + RADIUS Outpost，但官方明说「Currently, only authentication requests are supported.」，且只支持 EAP-TLS 和 PAP，MFA 码要拼在密码后面（`password;123456`）。
- https://docs.goauthentik.io/add-secure-apps/providers/radius/

用在 Linux SSH 上要在机器装 `pam_radius` 并改 PAM 栈，而且 RADIUS 只做认证、不提供 NSS（用户/uid/home 还得另外来源），所以实际上还是要配合本地账号或 SSSD。它的主场是 VPN 和网络设备，不是 Linux 登录。**本场景不推荐。**

### 3.6 路 F：`AuthorizedKeysCommand` 直接查 authentik API

不装 sssd，写一个小脚本放 `AuthorizedKeysCommand`，按用户名去 authentik 的 `/api/v3/core/users/?attributes={...}` 取 `sshPublicKey` 并按 `is_active` 过滤。接口规格、鉴权（Bearer service account token）、属性过滤语法在已有调研里都核过了，见 `docs/collab/research/authentik-attribute-write-api.md` §2。

优点：比 sssd 轻得多（一个脚本 + 两行 sshd_config），失效即时（禁用当场生效，不像 sssd 有缓存）。
缺点：机器要出站访问 IAM 且**每次登录都要**（IAM 挂了就登不上，要设计降级）；机器上要放一个 authentik API token（等于又引入一个长期凭证）；用户与本地账号的映射要自己维护。

评价：介于 B 和 D 之间的折中，适合我方完全掌控、但不想上 sssd 的机器。对九章不合适（出站依赖 + 放 token）。

---

## 4. 横向对比

| | A. RAC | B. SSH CA（step-ca） | C. opkssh | D. LDAP+SSSD | E. RADIUS | F. AuthorizedKeysCommand |
|---|---|---|---|---|---|---|
| 目标机要装什么 | 无 | 无 | opkssh 二进制 + 系统用户 | sssd 包 + PAM 改造 | pam_radius + PAM 改造 | 一个脚本 |
| 目标机要改什么 | 无 | `sshd_config` 一行 `TrustedUserCAKeys` + reload | `sshd_config` 两行 + `/etc/opk/*` | `/etc/sssd/sssd.conf` + nsswitch/PAM | PAM 栈 | `sshd_config` 两行 |
| 需要 root | 否 | 是（改 sshd_config） | 是 | 是 | 是 | 是 |
| 机器要能访问 IAM | 否 | **否** | 是（拉 JWKS） | 是（LDAPS 636，长期） | 是（RADIUS 1812） | 是（每次登录） |
| 网络方向 | 我方 outpost → 机器（同现状） | 无新增 | 机器 → IdP | 机器 → outpost | 机器 → outpost | 机器 → IAM |
| 人走了多久失效 | 立即（authentik 禁用） | ≤ 证书有效期（默认 16h，可调短） | ≤ 公钥有效期（默认 24h，可调） | 立即，**但必须加 `ak-active=true` 过滤，否则不失效** | 立即 | 立即 |
| 机器侧能分清人 | 否（仍是 root） | 是（sshd 日志记 cert ID/serial） | 是 | 是（真实 Unix 账号） | 部分 | 是 |
| 支持 bot 程序化 SSH | 否 | 是（paramiko 可加载证书） | 【拿不准】 | 是 | 是 | 是 |
| 需要对方配合的程度 | 零 | 一次性改一行配置 + reload sshd | 装软件、建系统用户、开出站 | 改造整台机器的认证栈 | 改 PAM | 改 sshd + 开出站 + 放 token |
| 额外要维护的组件 | RAC outpost | step-ca | 无（服务端自包含） | LDAP outpost | RADIUS outpost | 无 |

bot 侧可行性补充：paramiko 支持客户端证书认证，`PKey.load_certificate()` 的 docstring 写明「For certificates, however, this can be used on the client side to offer authentication requests to the server based on certificate instead of raw public key.」—— 本机 paramiko 2.9.3 上已确认该 API 存在。【实测】所以路 B 下让 bot 也持短期证书（配一个续签循环）在技术上没有障碍。

---

## 5. 落地前必须先确认的前提

这些都是「不确认就别往下排期」的项，目前**全部未确认**（本次调研不允许触网验证）。

| # | 前提 | 为什么卡脖子 | 怎么确认 |
|---|---|---|---|
| P1 | 九章这台机的所有权与管理权：是我们租的裸机，还是合作方的机器/容器切片 | 决定 A 之外的所有方案是否可谈 | 问对方（见 §7） |
| P2 | 我方在九章有没有真正的 root（能改 `/etc/ssh/sshd_config` 并 reload sshd） | 路 B/C/D/E/F 的共同前提 | 问对方；`ssh -p 30019` 登录后看 `id` 与 sshd 配置可写性（需由 dev 执行，本次不做） |
| P3 | `221.199.124.79:30019` 这个高位端口背后是不是容器/NAT 转发；sshd 是宿主机的还是容器里的 | 若是容器，改 sshd 配置可能被镜像重建覆盖，CA 信任配置不持久 | 问对方 |
| P4 | 九章能否出站访问 `iam.wuji-tech.com`（443/636） | 直接决定 C/D/F 能不能用；B 不受影响 | 由对方在机器上 `curl -sI` / `nc -vz` 一次即可 |
| P5 | `iam.wuji-tech.com` 部署在哪（境内/境外）、有没有公网入口 | 若在境外，国内机器的长连接稳定性存疑，D 基本出局 | 问 IT |
| P6 | 九章上除了我们，还有谁的公钥在 `authorized_keys` / `authorized_keys2` | 评估「改成只信 CA」会不会踢掉别人 | 问对方 |
| P7 | 泰国机（`203.156.3.194:40002`，用户 `wuji`）和 SGP（`43.98.203.59`）分别归谁管 | 这两台如果是我方的，可以先拿它们试点，不必等九章 | 内部确认 |
| P8 | IT 侧是否愿意为 SSH 场景在 authentik 建 OIDC application + 开 service account | 路 B/C/F 都要 | 问 IT，可复用 `authentik-attribute-write-api.md` 里的接口申请模板 |
| P9 | authentik 2025.6.4 的 OIDC discovery 与 claims 能否直接喂给 step-ca 的 OIDC provisioner | §3.2 标了【拿不准】 | 搭一套 step-ca 在测试机上对接一次 |

---

## 6. 分档建议

### 第 0 档：先补齐现状的账（只改我方，今天就能做）

不碰任何机器，把「谁触发了任务」这条线补完整，作为后续所有方案的兜底与过渡：

1. `core/ssh_transfer/cli.py:40` 建 job 时不传 `open_id`，CLI 起的任务 `created_by` 为空。补一个 `--operator` 参数或读系统用户写进去。同理检查其余 CLI（`core/transfer/cli.py:69`、`core/cpfs_dataflow/cli.py:100`、`core/vepfs_dataflow/cli.py:72`、`core/pfs_transfer/cli.py:41`）。
2. 在远端工作目录（`JIUZHANG_WORK_DIR=$HOME/.jiuzhang_jobs/<job_id>/`）里落一个只含 job_id + 触发人 open_id + 时间的纯文本 marker，让机器侧也能自证「这个目录是谁的任务产生的」。不含任何凭证。
3. 把 `JIUZHANG_SSH_KEY_ENC` 和 `SGP_SSH_KEY_ENC` 拆成两把不同的钥匙，切断「SGP 失守 = 九章失守」。这一步需要对方在 `authorized_keys2` 里换一次公钥，是本档唯一需要对方配合的动作。

这一档不提供任何「人走了自动失效」的能力，它只是把现有的归属链补完，成本几乎为零。

### 第 1 档：RAC，给人用（机器零改动）

如果 P1/P2 的答案是「机器不是我们的 / 没有 root」，那么在九章上唯一可做的就是路 A。部署一个 RAC outpost，把九章配成 endpoint，凭证（现有那把私钥）存在 authentik 的 property mapping 里，人通过 authentik 登录后在浏览器里开终端。

得到：谁能登由 authentik 授权，离职禁用即刻失效，authentik 有访问事件记录，私钥不再散落在各人手里。
得不到：机器侧日志仍是 root；bot 的程序化 SSH 不受影响也不受管（还是那把 key）。

这是「对方零配合」条件下能拿到的最好结果，建议无论最终走哪条路都先做，因为它和路 B 不冲突、可叠加。

### 第 2 档：SSH CA（机器只改一行）

前提是 P2 为真。步骤：
1. 我方部署 step-ca，OIDC provisioner 指向 authentik，用 authentik 的 group 映射成 SSH principals。
2. 九章侧一次性动作：放一个 CA 公钥文件，`sshd_config` 加 `TrustedUserCAKeys /etc/ssh/wuji_user_ca.pub`，`systemctl reload sshd`。可选加 `AuthorizedPrincipalsFile` 控制哪些 principal 能登哪个账号。
3. 人：`step ssh login` 换 8 小时证书后正常 `ssh`。
4. bot：改成持短期证书（paramiko `load_certificate`）+ 续签循环，或者作为过渡继续用那把 key 但缩小到专用非 root 账号。
5. 观察期过后再删掉 `authorized_keys2` 里的长期公钥 —— 这一步之前，长期公钥仍是后门，方案等于没落地。

这是我的推荐主线：对方的配合度只有「改一行配置 + reload」，机器不需要访问 IAM，离职失效靠证书自然到期，审计靠 sshd 日志里的 cert ID/serial。

### 第 3 档：LDAP + SSSD（彻底，但只对我们自己的机器）

只在 P1 确认「机器是我们的」且 P4/P5 确认网络通的前提下考虑，优先拿 SGP 或泰国机试点，不要拿九章开刀。落地时 `ldap_access_order = filter` + `ldap_access_filter = ak-active=true` 是必选项，不是可选项（§3.4）。sudo 规则仍需在机器上自己配（authentik 的 LDAP 不提供）。

Teleport 不进候选：社区版接不上 Authentik（§3.2 B3），企业版是另一个采购决策。

---

## 7. 需要向九章确认的问题清单（可直接转发）

> 我们这边在做内部账号统一管理，想把访问贵方 GPU 集群的登录方式也纳进来，目的是「人员变动后权限自动失效」和「登录记录能对应到具体的人」。在提具体方案之前，有几个情况想先跟你们确认一下，麻烦帮忙看下：
>
> 1. 我们现在通过 `221.199.124.79` 的 30019 端口以 root 登录，公钥放在 `~/.ssh/authorized_keys2`。想确认这台机器（或这个实例）是由贵方统一管理，还是交给我们自行管理？
> 2. 我们这个 root 账号是整机的 root，还是容器/实例内的 root？如果是容器，容器重建后 `/etc` 下的改动会不会丢失？
> 3. 我们能否修改 `/etc/ssh/sshd_config` 并重启（reload）sshd？如果不能，是否可以由贵方代为修改？
> 4. 如果我们提供一个 SSH CA 公钥文件，贵方能否在 sshd 配置里加一行 `TrustedUserCAKeys` 指向它？改完之后，我方人员用有效期几小时的临时证书登录，贵方不需要再为我方增删任何公钥，我方人员离职也不需要通知贵方。这是我们最希望走的方式。
> 5. 这台机器能否访问外网 HTTPS？具体想确认能不能访问 `iam.wuji-tech.com` 的 443 端口（只需要 `curl -sI https://iam.wuji-tech.com` 看一下有没有响应）。这决定了我们是否还有另一种方案可选。
> 6. 目前 `~/.ssh/authorized_keys` 和 `authorized_keys2` 里除了我们的公钥，还有哪些方的公钥？我们后续如果收紧登录方式，想确认不会影响到其他人。
> 7. 贵方对这台机器的登录行为有没有自己的审计要求或现成的日志留存机制？如果有，我们尽量配合现有机制，不重复造一套。
> 8. 除了 SSH，贵方有没有提供跳板机、堡垒机或 VPN 之类的统一入口？如果有，我们优先走贵方的入口。
>
> 这几条确认完我们再出具体方案，不会在没有对齐前改动任何配置。

---

## 8. 出处清单

| # | 内容 | 出处 |
|---|---|---|
| A1 | authentik LDAP provider 能力与限制（bind 模式、MFA、outpost） | https://docs.goauthentik.io/add-secure-apps/providers/ldap/ |
| A2 | authentik × sssd 集成全文（sssd.conf、`sshPublicKey`、`ak-active=true` 过滤、不支持 sudo/automount/netgroups/Kerberos） | https://integrations.goauthentik.io/infrastructure/sssd/ ；原文见 https://raw.githubusercontent.com/goauthentik/authentik/main/website/integrations/infrastructure/sssd/index.mdx |
| A3 | authentik RADIUS provider（仅认证请求、仅 PAP/EAP-TLS） | https://docs.goauthentik.io/add-secure-apps/providers/radius/ |
| A4 | authentik RAC provider 概述 | https://docs.goauthentik.io/add-secure-apps/providers/rac/ |
| A5 | RAC 部署步骤（outpost / endpoint / property mapping） | https://docs.goauthentik.io/add-secure-apps/providers/rac/how-to-rac/ |
| A6 | RAC 公钥认证 | https://docs.goauthentik.io/add-secure-apps/providers/rac/rac-public-key/ |
| A7 | RAC 于 2025.2 转为开源免费 | https://goauthentik.io/blog/2025-02-04-open-source-rac-and-pricing-support-updates/ |
| A8 | authentik 功能总览（无 SSH 证书签发） | https://goauthentik.io/features/ |
| A9 | issue：Docker SSH Outpost 用短期 SSH 证书 | https://github.com/goauthentik/authentik/issues/2916 |
| A10 | issue：用户 Profile 里加 SSH Key | https://github.com/goauthentik/authentik/issues/2145 |
| A11 | RAC property mapping 传 Guacamole 连接参数的已知问题 | https://github.com/goauthentik/authentik/issues/15783 |
| O1 | `sshd_config` 中 `TrustedUserCAKeys` / `RevokedKeys` / `AuthorizedPrincipalsFile` / `AuthorizedKeysCommand` / `AuthorizedKeysCommandUser` 原文 | https://man.openbsd.org/sshd_config |
| O2 | sshd 日志中证书 ID / serial / CA 指纹的形态与审计用法 | https://keybase-ssh-ca-bot.readthedocs.io/en/latest/sshca.html |
| S1 | step-ca provisioner（OIDC 字段、SSH 用户证书、admins 与 host 证书） | https://smallstep.com/docs/step-ca/provisioners/ |
| S2 | step-ca SSH 证书登录教程（服务端 `TrustedUserCAKeys` / `HostCertificate`、客户端 `@cert-authority`） | https://smallstep.com/docs/tutorials/ssh-certificate-login/ |
| S3 | `step ssh certificate` 参考（默认 16 小时） | https://smallstep.com/docs/step-cli/reference/ssh/certificate/ |
| S4 | smallstep 商业 SSH 产品的工作方式（含主机 agent / NSS / PAM，与开源 step-ca 不同） | https://smallstep.com/docs/ssh/how-it-works/ |
| V1 | Vault SSH signed certificates（CA 模式、`TrustedUserCAKeys`、CA 公钥免鉴权取用） | https://developer.hashicorp.com/vault/docs/secrets/ssh/signed-ssh-certificates |
| V2 | Vault SSH secrets engine 总览 | https://developer.hashicorp.com/vault/docs/secrets/ssh |
| T1 | Teleport SSO：SAML/OIDC 连接器为企业版功能 | https://goteleport.com/docs/zero-trust-access/sso/integrate-idp/ |
| T2 | Teleport 社区版 GitHub SAML SSO 限制 | https://goteleport.com/blog/community-github-saml-sso/ |
| K1 | opkssh 仓库与服务端安装/配置（`AuthorizedKeysCommand`、`/etc/opk/providers`、`auth_id`、过期策略、支持 Authentik） | https://github.com/openpubkey/opkssh |
| K2 | Cloudflare 开源 opkssh 的说明 | https://blog.cloudflare.com/open-sourcing-openpubkey-ssh-opkssh-integrating-single-sign-on-with-ssh/ |
| C1 | 本仓库：九章连接层（取钥回落、root、RejectPolicy） | `core/jiuzhang_transfer/engine.py:33-84` |
| C2 | 本仓库：三台机的默认地址与用户 | `config/settings.py:382-386, 394-396, 411-431` |
| C3 | 本仓库：任务归属记录 `created_by` | `core/jiuzhang_transfer/orchestrator.py:72,86`；`core/ssh_transfer/orchestrator.py:80,87-88,105` |
| C4 | 本仓库：CLI 入口未传 `open_id` | `core/ssh_transfer/cli.py:40` |
| C5 | 已有调研：authentik 用户属性读写 API 与权限模型 | `docs/collab/research/authentik-attribute-write-api.md` |

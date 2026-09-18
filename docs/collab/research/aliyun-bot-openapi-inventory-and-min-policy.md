# AIOps Bot 阿里云 OpenAPI 全量盘点 + 最小权限 RAM 策略

- 调查人：researcher（只读，未调用任何云 API，未改任何源码）
- 代码基线：`langchaindev` `feat/multi-platform-delivery` @ `7462714`；`infra` 工作区快照
- 方法：逐文件读代码取 Action（`call_api` 一律看传入的 action 字符串，不看函数名）；Action 名与资源 ARN 格式再回官方文档核对
- 可信度标记：`【代码】`=在仓库里直接读到 / `【文档】`=官方帮助中心明说（附 URL）/ `【推测】`=未证实

> ⚠️ 阻塞项：**SSH 到 bot-server 被权限策略拦下**（`Production Reads`）。因此
> ① 无法反查容器里 `alibabacloud_hcs_mgw20240626` / `volcenginesdkvepfs` 的 model 定义；
> ② 无法确认线上 `.env` 实际配了哪几把 AK。本报告全部为代码侧分析。需要真机取证的项在第 7 节单列。

---

## 0. 结论速览

1. **代码里没有任何 PAI DLC OpenAPI 调用。** 任务书里的「DLC 作业查询」在实现上是 **Prometheus 指标**
   （`AliyunPaidlc_*` 系列 PromQL），不是 `pai-dlc` SDK。因此策略里不需要 `paidlc:*`，需要的是
   Prometheus/CMS 读权限。这是本次盘点最容易写错的一条。
2. **bot 实际用的是 4 把阿里云 AK，不是 2 把**（任务书只提到 2 把）：
   `ALIYUN_ACCESS_KEY_*`、`PAI_DSW_ACCESS_KEY_*`、`ALIYUN_BOT_MASTER_AK_*`、`ALIYUN_1949_ACCESS_KEY_*`。
   其中 **`PAI_DSW_ACCESS_KEY_*` 才是绝大多数云操作的实际执行者**（`aliyun_client_factory._resolve_cred`
   第 ③ 档兜底 → DSW/ECS/OSS/SLS/NAS/MGW 全走它）。只替 `ALIYUN_ACCESS_KEY_*` 是替不干净的。
3. 建议拆 **3 个身份 / 3 条自定义策略**（都在 6144 字符内，实测 pretty 3.7KB / 3.9KB / 0.6KB）：
   - `aiops-bot-ops` → 给 `PAI_DSW_ACCESS_KEY_*`（运维面：DSW/ECS/OSS 只读/SLS/NAS/MGW/Prometheus + RAM 只读）
   - `aiops-bot-ram-write` → 给 `ALIYUN_ACCESS_KEY_*`（建号 + 发凭证 + policy 前缀限定）
   - `aiops-bot-sts` → 给 `ALIYUN_BOT_MASTER_AK_*`（替掉 `AliyunSTSAssumeRoleAccess` + `RAMReadOnlyAccess`）
4. **Master AK 那把必须保留**，不能被前两条替掉：它是多租户隔离链的唯一入口
   （`utils/aliyun_sts.py`），而且它的 `sts:AssumeRole` 一旦和 RAM 写权限同居一把 AK，
   「收窄」就失去意义（见第 5 节）。
5. **三处「串味」调用**让身份拆分不彻底，建议 dev 改代码（见 5.3）：
   `prometheus._auth()`、`jiuzhang_transfer/verify.py`、`temp_ak_issuance/orchestrator._probe_region_once`
   都直接读 `ALIYUN_ACCESS_KEY_*`，把监控读 / OSS 列举塞进了「RAM 写」那把 AK。

---

## 1. 调用点全表（阿里云）

### 1.1 PAI DSW — SDK `alibabacloud_pai_dsw20220101`，版本 `2022-01-01`，endpoint `pai-dsw.{region}.aliyuncs.com`

| 文件:行 | SDK 方法 | Action | 资源粒度 |
|---|---|---|---|
| `tools/aliyun/pai_dsw.py:149` | `list_instances` | `paidsw:ListInstances` | `*` |
| `tools/aliyun/pai_dsw.py:180` | `get_instance` | `paidsw:GetInstance` | `*` |
| `tools/aliyun/pai_dsw.py:201` | `start_instance` | `paidsw:StartInstance` | `*` |
| `tools/aliyun/pai_dsw.py:209` | `stop_instance` | `paidsw:StopInstance` | `*` |
| `tools/aliyun/pai_dsw.py:219` | `get_instance`（stop 前查状态） | `paidsw:GetInstance` | `*` |
| `tools/aliyun/pai_dsw.py:226` | `delete_instance` | `paidsw:DeleteInstance` | `*` |
| `tools/aliyun/pai_dsw.py:238` | `list_instances`（discover） | `paidsw:ListInstances` | `*` |
| `tools/aliyun/pai_dsw.py:295` | `list_ecs_specs` | `paidsw:ListEcsSpecs` | `*` |
| `tools/aliyun/pai_dsw.py:357` | `create_instance` | `paidsw:CreateInstance` | `*` |
| `tools/aliyun/pai_dsw.py:396,442` | `list_instances`（`list_dsw_resources` / `get_running_gpu_instances`） | `paidsw:ListInstances` | `*` |
| `tools/aliyun/ram.py:30` | `list_instances`（从 DSW 提取团队成员） | `paidsw:ListInstances` | `*` |
| `tools/aliyun/gpu_distribution.py:96` | `list_instances`（卡分布补 DSW 占卡） | `paidsw:ListInstances` | `*` |
| `core/dsw_scheduler.py:232/535/633` | 经 `manage_pai_dsw` 的 get / create_json / stop | 同上 | `*` |
| `tools/aliyun/dsw_inspector.py:172`、`cluster_health.py:53` | 经 `_list_instances` / `get_running_gpu_instances` | `paidsw:ListInstances` | `*` |

**为什么只能 `*`**：PAI DSW 的授权信息表里资源类型写的是「*全部资源」，条件关键字 `*`，
不支持资源级授权。【文档】[ListInstances 授权信息](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-listinstances)、
[GetInstance 授权信息](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-getinstance)。
PAI 只提供 **Condition 收窄**（`pai:Accessibility`=PUBLIC/PRIVATE、`pai:EntityAccessType`=CREATOR/OTHERS、
`acs:SourceIp`）【文档】[PAI 资源访问授权](https://help.aliyun.com/zh/pai/set-access-permissions-for-resources-based-on-the-condition-attribute-of-ram)。
**本项目用不了这两个 Condition**：bot 要管全公司所有人的实例（早报、巡检、代建），
`EntityAccessType=CREATOR` 会把它自己关在门外。可用的只有 `acs:SourceIp`（见 4.4）。

### 1.2 PAI DLC —— **无 OpenAPI 调用**

`tools/aliyun/gpu_distribution.py:33` 的「用户在算卡数」是 PromQL
`count by (jobUserId,regionId)(AliyunPaidlc_CARD_GPU_PIP_TENSOR_ACTIVE)`，走
`tools/aliyun/cluster_mfu._grouped` → `tools/aliyun/prometheus._query_instant` → HTTP。【代码】
`cluster_mfu.py:183-213,443-449` 的 25 条 PromQL 同理。整个仓库没有 `alibabacloud_pai_dlc*` 的 import。【代码】

### 1.3 ECS — SDK `alibabacloud_ecs20140526`，版本 `2014-05-26`

| 文件:行 | SDK 方法 | Action |
|---|---|---|
| `tools/aliyun/ecs.py:83,109` | `describe_instances` | `ecs:DescribeInstances` |
| `tools/aliyun/ecs.py:154` | `run_instances` | `ecs:RunInstances` |
| `tools/aliyun/ecs.py:168` | `start_instance` | `ecs:StartInstance` |
| `tools/aliyun/ecs.py:176` | `stop_instance` | `ecs:StopInstance` |
| `tools/aliyun/ecs.py:184` | `reboot_instance` | `ecs:RebootInstance` |
| `tools/aliyun/ecs.py:192` | `delete_instance` | `ecs:DeleteInstance` |

资源粒度：实例类操作可收到 `acs:ecs:*:<UID>:instance/*`，但 `RunInstances` 还要摸 image /
vswitch / securitygroup / 磁盘等多种资源 ARN，逐个列出既长又脆。建议 `acs:ecs:*:<UID>:*`
（仍然把跨账号挡在外面）。**更好的选择是根本不给 ECS 写**——见 4.3。

### 1.4 OSS — SDK `oss2`（数据面 HTTP 签名，不是 OpenAPI）

| 文件:行 | oss2 调用 | Action |
|---|---|---|
| `tools/aliyun/oss.py:91` | `BucketIterator(service)` | `oss:ListBuckets` |
| `tools/aliyun/oss.py:113,133` | `ObjectIterator` | `oss:ListObjects` |
| `tools/aliyun/oss.py:281` | `get_bucket_location()` | `oss:GetBucketLocation` |
| `tools/aliyun/oss.py:351` | `ObjectIteratorV2`（`estimate_prefix`，审批门估算） | `oss:ListObjects` |
| `tools/aliyun/oss.py:382,397,637` | `list_objects_v2` | `oss:ListObjects` |
| `tools/aliyun/oss.py:691,718` | `get_object`（读 lerobot `info.json` / hdf5 头，识别数据集模态） | `oss:GetObject` |
| `tools/aliyun/oss.py:200` | `create_bucket` | `oss:PutBucket` |
| `tools/aliyun/oss.py:213` | `put_object` | `oss:PutObject` |
| `tools/aliyun/oss.py:227` | `delete_object` | `oss:DeleteObject` |
| `tools/aliyun/oss.py:237` | `delete_bucket` | `oss:DeleteBucket` |
| `core/dataset_dashboard.py:148` | `list_objects_v2`（大盘存在性探测） | `oss:ListObjects` |
| `core/ssh_transfer/verify.py:44` | `ObjectIterator`（段2 校验源清单） | `oss:ListObjects` |
| `core/jiuzhang_transfer/verify.py:40` | `ObjectIteratorV2`（九章校验源清单，**用 `ALIYUN_ACCESS_KEY_*`**） | `oss:ListObjects` |
| `core/temp_ak_issuance/orchestrator.py:191` | `get_bucket_location()`（桶地域+存在性探测，**用档案 AK = `ALIYUN_ACCESS_KEY_*`**） | `oss:GetBucketLocation` |
| `core/capacity_monitor.py:200` | 经 `oss.compute_nested_sizes` | `oss:ListObjects` + `oss:GetObject` |

资源粒度：OSS 支持资源级，**能且应该按桶枚举** —— `acs:oss:*:<UID>:<bucket>` (桶级动作) +
`acs:oss:*:<UID>:<bucket>/*` (对象级动作)。`oss:ListBuckets` 是账号级、只能 `acs:oss:*:<UID>:*`。

`ossutil`（SGP / 泰国 / 九章三台机器）**不用 bot 的 AK**——它读各机 `~/.ossutilconfig`
（`core/ssh_transfer/engine_ssh.py`、`core/jiuzhang_transfer/engine.py`），属于独立凭证面，
不在本策略范围内。【代码】

### 1.5 SLS — SDK `aliyun-log-python-sdk`（`LogClient`）

| 文件:行 | 方法 | Action | 资源 |
|---|---|---|---|
| `tools/aliyun/sls.py:74` | `list_project()` | `log:ListProject` | `acs:log:*:<UID>:project/*` |
| `tools/aliyun/sls.py:92` | `list_logstore(project)` | `log:ListLogStores` | `acs:log:*:<UID>:project/*/logstore/*` |
| `tools/aliyun/sls.py:107` | `get_log(...)` | `log:GetLogStoreLogs` | 同上 |
| `tools/aliyun/sls.py:140` | `create_project` | `log:CreateProject` | 同上 |
| `tools/aliyun/sls.py:150` | `create_logstore` | `log:CreateLogStore` | 同上 |

【文档】资源写法见 [SLS RAM 自定义授权](https://help.aliyun.com/zh/sls/use-custom-policies-to-grant-permissions-to-a-ram-user)：
`log:ListProject` 用 `acs:log:*:*:project/*`，logstore 级用 `acs:log:*:*:project/<P>/logstore/<L>`。
**建议不给 `log:CreateProject` / `log:CreateLogStore`**（只有 Agent 工具的 confirm 分支会用，
而 `BaseOpsTool` 明确不是安全边界，见 CLAUDE.md）。

### 1.6 Prometheus / ARMS / CMS —— **Basic Auth，不是 OpenAPI 签名**

`tools/aliyun/prometheus.py:92` `_auth()` 返回 `(ALIYUN_ACCESS_KEY_ID, ALIYUN_ACCESS_KEY_SECRET)`，
直接 `requests.get(PROMETHEUS_URL + "/api/v1/query", auth=...)`（`:102,121,742`）。【代码】

【文档】[ARMS HTTP API 地址](https://www.alibabacloud.com/help/zh/arms/prometheus-monitoring/http-api-urls)：
AK 作 Basic Auth 用户名/密码，该 RAM 用户需 `AliyunPrometheusMetricReadAccess` 或
`AliyunCloudMonitorFullAccess`。前者的实际内容【文档】
[AliyunPrometheusMetricReadAccess](https://help.aliyun.com/zh/ram/developer-reference/aliyunprometheusmetricreadaccess)：

```
cms:ListPrometheusInstances, cms:GetPrometheusInstance, cms:GetPrometheusView,
cms:ListPrometheusViews, cms:ListPrometheusMetrics,
log:QueryMetrics, log:QueryPrometheusMetrics, log:GetLogStoreLogs    (Resource: *)
```

这 8 条已是官方最小读集合，资源只能 `*`（系统策略本身就是 `*`）。我直接内联进自定义策略，
**不要挂 `AliyunCloudMonitorFullAccess`**（那是 Full）。

覆盖的功能：`cluster_mfu`（~25 PromQL/区）、`gpu_distribution`、`dsw_inspector`、
`cluster_health`、`gpu_advisor`、`gpu_training_advisor`、`dsw_scheduler` 的 GPU 空闲判断
（`core/dsw_scheduler.py:257`）。【代码】

### 1.7 NAS / CPFS DataFlow — 通用 `call_api`，产品 `NAS`，版本 `2017-06-26`，RPC

`core/cpfs_dataflow/engine_nas.py:98-112` 的 `_call(client, action, query)` 把 action 当字符串传进
`om.Params(action=action, version=NAS_VERSION, ...)` → **看的是调用点传的字符串**：

| 文件:行 | action 字符串 | Action |
|---|---|---|
| `engine_nas.py:151,201` | `"DescribeDataFlows"` | `nas:DescribeDataFlows` |
| `engine_nas.py:182` | `"CreateDataFlow"` | `nas:CreateDataFlow` |
| `engine_nas.py:229` | `"DeleteDataFlow"` | `nas:DeleteDataFlow` |
| `engine_nas.py:243` | `"DescribeFileSystems"` | `nas:DescribeFileSystems` |
| `engine_nas.py:329` | `"CreateDataFlowTask"` | `nas:CreateDataFlowTask` |
| `engine_nas.py:347` | `"DescribeDataFlowTasks"` | `nas:DescribeDataFlowTasks` |
| `engine_nas.py:389` | `"CancelDataFlowTask"` | `nas:CancelDataFlowTask` |

资源粒度：**能收到文件系统级**。【文档】[CreateDataFlowTask 授权信息](https://help.aliyun.com/zh/cpfs/cpfsonecs/developer-reference/api-nas-2017-06-26-createdataflowtask-cpfs)
给的是 `acs:nas:{#regionId}:{#accountId}:filesystem/{#filesystemId}`。
把 `CPFS_FILE_SYSTEM_IDS` 里的 fs-id 逐个列出即可（线上已知至少 `bmcpfs-00000ub3ici1dnniit2i0`）。
`DescribeFileSystems`（枚举路径）在 NAS 授权表里资源类型是 FileSystem，
`acs:nas:{region}:{account}:filesystem/*`【文档】[NAS 授权信息](https://www.alibabacloud.com/help/zh/nas/developer-reference/api-nas-2017-06-26-ram)
——只在 `CPFS_FILE_SYSTEM_IDS` 留空时走，如果线上显式配了 IDS，这条可以不给。

⚠️ `nas:CreateDataFlow` / `nas:DeleteDataFlow` 是**破坏性最高的一对**：CLAUDE.md 明确
「CreateDataFlow 会清空 Fileset」。代码侧已限制只对 `bmcpfs-` 智算版开放且「优先复用现有绑定」
（`engine_nas.create_dataflow` + `orchestrator._cleanup_ephemeral`），但**策略侧应把它俩
单独一条 statement、只列智算版 fs-id**，别和 `DescribeDataFlows` 混在一起。

### 1.8 在线迁移服务 hcs_mgw — SDK `alibabacloud_hcs_mgw20240626`，endpoint `cn-beijing.mgw.aliyuncs.com`

| 文件:行 | SDK 方法 | Action【文档】 | 资源 |
|---|---|---|---|
| `core/transfer/engine_mgw.py:60` | `create_address(userid, CreateAddressRequest(import_address=...))` | `mgw:CreateImportAddress` | `acs:mgw:*:<UID>:address/*` |
| `core/transfer/engine_mgw.py:131` | `verify_address(userid, name)` | `mgw:VerifyImportAddress` | `acs:mgw:*:<UID>:address/*` |
| `core/transfer/engine_mgw.py:180` | `create_job(userid, CreateJobRequest(import_job=...))` | `mgw:CreateImportJob` | `acs:mgw:*:<UID>:job/*` |
| `core/transfer/engine_mgw.py:188` | `update_job(userid, name, ...)`（`IMPORT_JOB_LAUNCHING`） | `mgw:UpdateImportJob` | `acs:mgw:*:<UID>:job/*` |
| `core/transfer/engine_mgw.py:201` | `get_job(userid, name, GetJobRequest())` | `mgw:GetImportJob` | `acs:mgw:*:<UID>:job/*` |
| `core/transfer/engine_mgw.py:207` | `list_job_history(userid, name, ...)` | `mgw:ListImportJobHistory` | `acs:mgw:*:<UID>:job/*` |

【文档】[在线迁移服务 RAM 权限策略](https://help.aliyun.com/zh/data-online-migration/permission-policy)。
两次独立抓取都给出同一组 **`Import` 中缀** 的 Action 名（`mgw:CreateImportAddress` 而非
`mgw:CreateAddress`），与 SDK 里 `import_address` / `import_job` 字段名、`IMPORT_JOB_*` 状态串一致，
交叉印证成立。资源 ARN：`acs:mgw:{region}:{account}:address/{name}`、`.../job/{name}`，
**region 段目前只支持通配符 `*`**。
迁移 job/address 名 = `job_id`（`transfer:` 前缀 `tr-` / `bkt-`），可以进一步收窄成
`address/tr-*` 之类，但 job_id 是 hash、前缀不完全稳定，我**不建议**这么收（收错=迁移静默失败）。

同一条链的调用方还有 `core/bucket_transfer/orchestrator.py:114,154`（同云 OSS→OSS 桶间迁移，
`src_scheme="oss"` 走 `create_oss_source_address`）。【代码】

**`ram:PassRole`：`create_oss_dest_address` / `create_oss_source_address` 传的是 RAM 角色名
（`TRANSFER_OSS_ROLE` / `BUCKET_TRANSFER_OSS_SRC_ROLE`，`engine_mgw.py:90-124`），由迁移服务
（信任主体 `mgw.aliyuncs.com`）去 assume。文档的准备工作页只列了 `ram:CreateRole/CreatePolicy/
AttachPolicyToRole/ListRoles`，**没明说要 `ram:PassRole`**。【推测】我在策略里给了
`ram:PassRole` 且**限定到这两个角色 ARN**：不需要时它是无害的冗余，需要时缺了会 403。

### 1.9 RAM / IMS

#### (a) 建号链 `core/ram_approval.py`（AK 来源：`ALIBABA_CLOUD_*` env → `settings.ALIYUN_ACCESS_KEY_*`，见 `permsync.make_ram_client:364-376`）

| 文件:行 | SDK 方法 | Action |
|---|---|---|
| `ram_approval.py:376,549` | `get_user` | `ram:GetUser` |
| `ram_approval.py:381,555` | `create_user` | `ram:CreateUser` |
| `ram_approval.py:672,762` | `update_user`（`_ensure_user_profile`） | `ram:UpdateUser` |
| `ram_approval.py:409` | `get_login_profile` | `ram:GetLoginProfile` |
| `ram_approval.py:414,580` | `create_login_profile` | `ram:CreateLoginProfile` |
| `ram_approval.py:589` | `update_login_profile`（火山路径镜像；阿里路径同名方法存在） | `ram:UpdateLoginProfile` |
| `ram_approval.py:431,601` | `add_user_to_group` | `ram:AddUserToGroup` |
| `ram_approval.py:445,618` | `create_access_key` | `ram:CreateAccessKey` |
| `ram_approval.py:681,1448` | `list_access_keys` | `ram:ListAccessKeys` |
| `ram_approval.py:782,793` → `_call_ims_api("SetVerificationInfo")` @ `:806-826` | IMS `2019-08-15` `SetVerificationInfo`，endpoint `ims.aliyuncs.com`（`_make_ims_client:828-840`） | **`ram:SetVerificationInfo`** |

⚠️ 这里有个容易写错的点：**IMS 的 Action 前缀是 `ram:`，不是 `ims:`**。
【文档】[SetVerificationInfo](https://help.aliyun.com/zh/ram/developer-reference/api-ims-2019-08-15-setverificationinfo)
授权表写的是 `ram:SetVerificationInfo`，访问级别 update，资源类型「*全部资源」（不支持资源级）。
（`ims:` 前缀是**智能媒体服务**，完全另一个产品，写错就是白给一个无关产品的权限、同时真权限还缺。）

#### (b) 只读查询 `core/ram_query.py`（AK：`PAI_DSW_ACCESS_KEY_*` 优先，回退 `ALIYUN_ACCESS_KEY_*`，`:59`）

`get_user`(:121) / `list_groups_for_user`(:78) / `get_login_profile`(:88) / `list_access_keys`(:97)
→ `ram:GetUser` / `ram:ListGroupsForUser` / `ram:GetLoginProfile` / `ram:ListAccessKeys`。【代码】

#### (c) 团队成员 / 卡分布姓名映射 `tools/aliyun/ram.py:68`（AK：`PAI_DSW_ACCESS_KEY_*` 优先，`:53`）

`list_users`（marker 分页）→ `ram:ListUsers`。【代码】
被 `gpu_distribution._name_map()`（`:67`）调用 —— 所以**运维那把 AK 也需要 RAM 只读**。

#### (d) OSS 权限同步 `core/oss_perm/permsync.py`

| 行 | 方法 | Action | 可收窄到 |
|---|---|---|---|
| `:403` | `list_users`（marker 分页） | `ram:ListUsers` | `user/*` |
| `:469,547(ram_actual :548)` | `get_policy(policy_type="Custom")` | `ram:GetPolicy` | `policy/wuji-oss-auto-*` |
| `:480` | `create_policy` | `ram:CreatePolicy` | 同上 |
| `:485` | `create_policy_version(set_as_default, rotate_strategy)` | `ram:CreatePolicyVersion` | 同上 |
| `:501` | `get_user` | `ram:GetUser` | `user/*` |
| `:507`（`--create-users`） | `create_user` | `ram:CreateUser` | `user/*` |
| `:512` | `attach_policy_to_user` | `ram:AttachPolicyToUser` | `user/*` + `policy/wuji-oss-auto-*` |
| `:553` | `list_policies(policy_type="Custom")` | `ram:ListPolicies` | `policy/*`（list 类不能按前缀收） |
| `:565` | `list_entities_for_policy` | `ram:ListEntitiesForPolicy` | `policy/wuji-oss-auto-*` |

策略名前缀硬编码 `POLICY_PREFIX = "wuji-oss-auto-"`（`permsync.py:64`）。【代码】

#### (e) 临时 AK 发放 `core/temp_ak_issuance/`（AK：每个账号档案自己的 `ak_id/ak_secret`，`accounts.py:87,125`）

| 文件:行 | 方法 | Action | 可收窄到 |
|---|---|---|---|
| `issuer.py:122` | `get_user` | `ram:GetUser` | `user/tempak-*` |
| `issuer.py:126` | `create_user` | `ram:CreateUser` | `user/tempak-*` |
| `issuer.py:140` | `get_policy` | `ram:GetPolicy` | `policy/temp-ak-auto-*` |
| `issuer.py:141,183` | `create_policy_version` | `ram:CreatePolicyVersion` | 同上 |
| `issuer.py:146` | `create_policy` | `ram:CreatePolicy` | 同上 |
| `issuer.py:154` | `attach_policy_to_user` | `ram:AttachPolicyToUser` | `user/tempak-*` + `policy/temp-ak-auto-*` |
| `issuer.py:161` | `create_access_key` | `ram:CreateAccessKey` | `user/tempak-*` |
| `issuer.py:105` → `aliyun_sts.assume_role_with_policy` | `sts:AssumeRole`（带 session policy） | **用的是 Master AK**，见 1.10 | `role/<TEMP_AK_OSS_ROLE_ARN>` |
| `cleanup.py:21` | `list_access_keys` | `ram:ListAccessKeys` | `user/tempak-*` |
| `cleanup.py:31` | `list_policy_versions` | `ram:ListPolicyVersions` | `policy/temp-ak-auto-*` |
| `cleanup.py:38` | `delete_policy_version` | `ram:DeletePolicyVersion` | 同上 |
| `cleanup.py:42` | `delete_policy` | `ram:DeletePolicy` | 同上 |
| `cleanup.py:70` | `update_access_key(status="Inactive")` | `ram:UpdateAccessKey` | `user/tempak-*` |
| `cleanup.py:75` | `delete_access_key` | `ram:DeleteAccessKey` | `user/tempak-*` |
| `cleanup.py:81` | `detach_policy_from_user` | `ram:DetachPolicyFromUser` | `user/tempak-*` + `policy/temp-ak-auto-*` |
| `cleanup.py:89` | `delete_user` | `ram:DeleteUser` | `user/tempak-*` |

**关键收窄结论**：`ram:DeleteUser` / `ram:DeleteAccessKey` / `ram:UpdateAccessKey` / `ram:DeletePolicy`
**在整个仓库里只出现在 `temp_ak_issuance/cleanup.py`**（已全仓 grep 验证）。
RAM 登录名前缀由 `AccountProfile.user_prefix` 固定为 `tempak-` / `tempak-1949-`
（`accounts.py:93,128`），策略名前缀 `temp-ak-auto-`（`policy.py:21`）。
所以**删除类权限可以收窄到 `user/tempak-*` + `policy/temp-ak-auto-*`**，
bot 永远删不掉一个真人的 RAM 账号或 AK —— 这是本次最有价值的一条收窄。

### 1.10 STS `utils/aliyun_sts.py`（AK：`ALIYUN_BOT_MASTER_AK_*`）

| 行 | 方法 | Action | 资源 |
|---|---|---|---|
| `:69` | `list_groups_for_user`（用户组 → 角色 ARN 映射） | `ram:ListGroupsForUser` | `acs:ram:*:<UID>:user/*` |
| `:131` | `assume_role(role_arn, session_name, duration)` | `sts:AssumeRole` | `acs:ram:*:<UID>:role/<ALIYUN_BOT_ROLE_MAPPING 里的每个角色>` + `role/<ALIYUN_BOT_ROLE_DEFAULT>` |
| `:183` | `assume_role(..., policy=session_policy)` | `sts:AssumeRole` | `acs:ram:*:<UID>:role/<TEMP_AK_OSS_ROLE_ARN 的角色名>` |

另外 `tools/aliyun/ram.get_ram_user_by_open_id` 只读 Redis 映射表，不打云 API。【代码】

### 1.11 其他 —— 无

- **资源中心 / 标签 / ACK**：全仓 grep `resourcemanager` / `TagResources` / `acs:cs` / `kubeconfig` **零命中**。
  `tools/ops/k8s.py` 是纯本地模拟（白名单 + 返回文案），不打任何云 API。【代码】
- 飞书多维表格（`capacity_bitable` / `dataset_dashboard`）走飞书 open API，不涉阿里云。【代码】

---

## 2. 最小权限策略 JSON

替换占位符：`<UID>`=阿里云主账号 UID、`<桶N>`=实际桶名、`<CPFS-fsid>`、`<角色名>`、`<允许组N>`
（= `FEISHU_RAM_APPROVAL_ALLOWED_GROUPS`）、`<bot自己的RAM用户名>`。
**JSON 本身不能有注释**，注释在每条 statement 前的说明里。

### 2.1 `aiops-bot-ops` —— 给 `PAI_DSW_ACCESS_KEY_*`（运维面）

实测长度（下方 JSON 原文）：pretty **3791** 字符 / compact 2543，均在 6144 内
（列 2 个桶、2 个 fs-id 的情况下）。每多一个桶约 +90 字符，按 pretty 计还能再加约 **26 个桶**。

Statement 用途对照：
1. PAI DSW 全部实例操作（早报 / 巡检 / 工单代建 / 停机）——资源只能 `*`（1.1）
2. ECS 增删启停（`manage_ecs`）
3. `oss:ListBuckets` 账号级列桶
4. OSS 按桶列举 / 读地域 / 读小文件（容量巡检、目录树、迁移估算、迁移校验、数据集大盘）
5. SLS 列 project
6. SLS 列 logstore + 查日志
7. Prometheus/CMS 只读（MFU 日报、卡分布、DSW 巡检、GPU 空闲判断）
8. NAS DataFlow **只读 + 提交/查/取消任务**（预热沉降）
9. NAS **CreateDataFlow / DeleteDataFlow**——单列，只给智算版 fs（破坏性最高）
10. MGW 数据地址
11. MGW 迁移任务
12. `ram:PassRole` 限定给迁移服务用的两个角色【推测，冗余无害】
13. RAM 只读（`gpu_distribution` 姓名映射、`ram_query` 查号）
14. **Deny**：一切 RAM 写 + `sts:AssumeRole`
15. **Deny**：OSS 删除 / 改 ACL / 改 Policy

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "paidsw:ListInstances",
        "paidsw:GetInstance",
        "paidsw:CreateInstance",
        "paidsw:StartInstance",
        "paidsw:StopInstance",
        "paidsw:DeleteInstance",
        "paidsw:ListEcsSpecs"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeInstances",
        "ecs:RunInstances",
        "ecs:StartInstance",
        "ecs:StopInstance",
        "ecs:RebootInstance",
        "ecs:DeleteInstance"
      ],
      "Resource": "acs:ecs:*:<UID>:*"
    },
    {
      "Effect": "Allow",
      "Action": "oss:ListBuckets",
      "Resource": "acs:oss:*:<UID>:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetBucketLocation",
        "oss:GetBucketInfo",
        "oss:ListObjects",
        "oss:GetObject"
      ],
      "Resource": [
        "acs:oss:*:<UID>:<桶1>",
        "acs:oss:*:<UID>:<桶1>/*",
        "acs:oss:*:<UID>:<桶2>",
        "acs:oss:*:<UID>:<桶2>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "log:ListProject",
      "Resource": "acs:log:*:<UID>:project/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "log:ListLogStores",
        "log:GetLogStoreLogs"
      ],
      "Resource": "acs:log:*:<UID>:project/*/logstore/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cms:ListPrometheusInstances",
        "cms:GetPrometheusInstance",
        "cms:GetPrometheusView",
        "cms:ListPrometheusViews",
        "cms:ListPrometheusMetrics",
        "log:QueryMetrics",
        "log:QueryPrometheusMetrics"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "nas:DescribeFileSystems",
        "nas:DescribeDataFlows",
        "nas:CreateDataFlowTask",
        "nas:DescribeDataFlowTasks",
        "nas:CancelDataFlowTask"
      ],
      "Resource": [
        "acs:nas:*:<UID>:filesystem/bmcpfs-00000ub3ici1dnniit2i0",
        "acs:nas:*:<UID>:filesystem/<其它CPFS-fsid>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "nas:CreateDataFlow",
        "nas:DeleteDataFlow"
      ],
      "Resource": "acs:nas:*:<UID>:filesystem/bmcpfs-00000ub3ici1dnniit2i0"
    },
    {
      "Effect": "Allow",
      "Action": [
        "mgw:CreateImportAddress",
        "mgw:GetImportAddress",
        "mgw:VerifyImportAddress",
        "mgw:UpdateImportAddress"
      ],
      "Resource": "acs:mgw:*:<UID>:address/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "mgw:CreateImportJob",
        "mgw:GetImportJob",
        "mgw:UpdateImportJob",
        "mgw:ListImportJobHistory",
        "mgw:GetImportJobResult"
      ],
      "Resource": "acs:mgw:*:<UID>:job/*"
    },
    {
      "Effect": "Allow",
      "Action": "ram:PassRole",
      "Resource": [
        "acs:ram:*:<UID>:role/<TRANSFER_OSS_ROLE>",
        "acs:ram:*:<UID>:role/<BUCKET_TRANSFER_OSS_SRC_ROLE>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ram:ListUsers",
        "ram:GetUser",
        "ram:ListGroupsForUser",
        "ram:GetLoginProfile",
        "ram:ListAccessKeys"
      ],
      "Resource": "acs:ram:*:<UID>:user/*"
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:Create*",
        "ram:Update*",
        "ram:Delete*",
        "ram:Attach*",
        "ram:Detach*",
        "ram:Add*",
        "ram:Remove*",
        "ram:Set*",
        "sts:AssumeRole"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Deny",
      "Action": [
        "oss:DeleteObject",
        "oss:DeleteObjectVersion",
        "oss:DeleteBucket",
        "oss:PutBucketAcl",
        "oss:PutBucketPolicy"
      ],
      "Resource": "*"
    }
  ]
}
```

> **刻意不给的**：`oss:PutObject` / `oss:PutBucket`、`log:CreateProject` / `log:CreateLogStore`。
> 这四个只被 Agent 工具 `manage_oss` / `manage_sls` 的 `confirm=true` 分支用到，而 `confirm`
> 是**模型自己填的参数**、不是安全边界（CLAUDE.md 已明写 `BaseOpsTool` 无鉴权无审计）。
> 不给的代价是工具返回一句 AccessDenied，收益是提示注入拿不到写权限。
> 如果确实要保留，各加一条 Allow、把 Resource 收到具体沙箱桶/project。

### 2.2 `aiops-bot-ram-write` —— 给 `ALIYUN_ACCESS_KEY_*`（建号 + 发凭证）

实测长度（下方 JSON 原文）：pretty **4159** / compact 2848（列 2 个组的情况下）。

Statement 用途对照：
1. RAM 用户生命周期**非删除部分**：建号审批（登录名人工填，无法前缀限定）+ oss_perm `--create-users` + temp_ak 建号
2. **删除类收窄到临时凭证前缀**：只有 `temp_ak_issuance/cleanup.py` 用（1.9e）
3. 入组：只允许审批白名单里的组
4. 自定义策略 CRUD：**只在两个前缀内**（`wuji-oss-auto-` / `temp-ak-auto-`）
5. `ram:ListPolicies`：list 类不支持前缀收窄，只能 `policy/*`（只读，可接受）
6. 挂/摘策略：user + 上述两个策略前缀 → **天然挂不了系统策略**（系统策略 ARN 是 `acs:ram:*:system:policy/...`，不在 Resource 里）
7. IMS 绑安全手机/邮箱（前缀 `ram:`，资源不支持收窄）
8. OSS 桶地域探测 + 九章校验列源（**串味调用**，见 5.3）
9. Prometheus/CMS 只读（**串味调用**，`prometheus._auth()` 用的就是这把 AK）
10. **Deny**：挂高危系统策略 + 挂自己这套策略
11. **Deny**：改自己这条策略（策略名前缀 `aiops-bot-*`）
12. **Deny**：动 bot 自己的 RAM 用户 / root
13. **Deny**：角色 / 用户组 / MFA / 密码策略 / AssumeRole

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ram:ListUsers",
        "ram:GetUser",
        "ram:CreateUser",
        "ram:UpdateUser",
        "ram:GetLoginProfile",
        "ram:CreateLoginProfile",
        "ram:UpdateLoginProfile",
        "ram:ListGroupsForUser",
        "ram:ListAccessKeys",
        "ram:CreateAccessKey"
      ],
      "Resource": "acs:ram:*:<UID>:user/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ram:DeleteUser",
        "ram:UpdateAccessKey",
        "ram:DeleteAccessKey"
      ],
      "Resource": [
        "acs:ram:*:<UID>:user/tempak-*",
        "acs:ram:*:<UID>:user/tempak-1949-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "ram:AddUserToGroup",
      "Resource": [
        "acs:ram:*:<UID>:user/*",
        "acs:ram:*:<UID>:group/<允许组1>",
        "acs:ram:*:<UID>:group/<允许组2>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ram:CreatePolicy",
        "ram:GetPolicy",
        "ram:CreatePolicyVersion",
        "ram:DeletePolicyVersion",
        "ram:ListPolicyVersions",
        "ram:DeletePolicy",
        "ram:ListEntitiesForPolicy"
      ],
      "Resource": [
        "acs:ram:*:<UID>:policy/wuji-oss-auto-*",
        "acs:ram:*:<UID>:policy/temp-ak-auto-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "ram:ListPolicies",
      "Resource": "acs:ram:*:<UID>:policy/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ram:AttachPolicyToUser",
        "ram:DetachPolicyFromUser"
      ],
      "Resource": [
        "acs:ram:*:<UID>:user/*",
        "acs:ram:*:<UID>:policy/wuji-oss-auto-*",
        "acs:ram:*:<UID>:policy/temp-ak-auto-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "ram:SetVerificationInfo",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetBucketLocation",
        "oss:ListObjects"
      ],
      "Resource": [
        "acs:oss:*:<UID>:*",
        "acs:oss:*:<UID>:*/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "cms:ListPrometheusInstances",
        "cms:GetPrometheusInstance",
        "cms:GetPrometheusView",
        "cms:ListPrometheusViews",
        "cms:ListPrometheusMetrics",
        "log:QueryMetrics",
        "log:QueryPrometheusMetrics",
        "log:GetLogStoreLogs"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:AttachPolicyToUser",
        "ram:AttachPolicyToGroup",
        "ram:AttachPolicyToRole"
      ],
      "Resource": [
        "acs:ram:*:system:policy/AdministratorAccess",
        "acs:ram:*:system:policy/AliyunRAMFullAccess",
        "acs:ram:*:system:policy/AliyunSTSAssumeRoleAccess",
        "acs:ram:*:system:policy/AliyunOSSFullAccess",
        "acs:ram:*:<UID>:policy/aiops-bot-*"
      ]
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:CreatePolicyVersion",
        "ram:DeletePolicyVersion",
        "ram:SetDefaultPolicyVersion",
        "ram:DeletePolicy",
        "ram:UpdatePolicyDescription"
      ],
      "Resource": "acs:ram:*:<UID>:policy/aiops-bot-*"
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:CreateAccessKey",
        "ram:DeleteAccessKey",
        "ram:UpdateAccessKey",
        "ram:CreateLoginProfile",
        "ram:UpdateLoginProfile",
        "ram:AttachPolicyToUser",
        "ram:AddUserToGroup",
        "ram:UpdateUser",
        "ram:DeleteUser"
      ],
      "Resource": [
        "acs:ram:*:<UID>:user/<bot自己的RAM用户名>",
        "acs:ram:*:<UID>:user/root"
      ]
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:CreateRole",
        "ram:UpdateRole",
        "ram:DeleteRole",
        "ram:AttachPolicyToRole",
        "ram:DetachPolicyFromRole",
        "ram:CreateGroup",
        "ram:DeleteGroup",
        "ram:AttachPolicyToGroup",
        "ram:RemoveUserFromGroup",
        "ram:DeleteVirtualMFADevice",
        "ram:UnbindMFADevice",
        "ram:SetSecurityPreference",
        "ram:SetPasswordPolicy",
        "sts:AssumeRole"
      ],
      "Resource": "*"
    }
  ]
}
```

> 第 12 条刻意**逐个列写动作**、没写 `ram:*`：`ram:*` 的 Deny 配上 `user/<某人>` 这种具体资源，
> 与 `ram:ListUsers`（请求资源是 `user/*`）的匹配语义在阿里云侧不够确定，
> 写宽了可能把 ListUsers 一起 Deny 掉、建号链直接瘫。**上线前请用 RAM 控制台的「策略模拟」
> 或一个测试子号验证一遍**（这条我标【推测】）。

### 2.3 `aiops-bot-sts` —— 给 `ALIYUN_BOT_MASTER_AK_*`

替掉现在的 `AliyunSTSAssumeRoleAccess`（= `sts:AssumeRole` on `*`，能 assume 账号里**任意**角色，
包括 admin 角色）+ `RAMReadOnlyAccess`（能读全账号 RAM 配置，含所有策略文档）。
pretty **762** 字符 / compact 510。

Statement 用途对照：
1. 只能 assume `ALIYUN_BOT_ROLE_MAPPING` / `ALIYUN_BOT_ROLE_DEFAULT` 里列的角色 + 临时 AK 的宽 OSS 角色
2. `utils/aliyun_sts._list_user_groups` 与 `_role_arn_for_user` 需要的 RAM 只读（**不是全量 RAMReadOnly**）
3. Deny 兜底

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "acs:ram:*:<UID>:role/BotRole-Default",
        "acs:ram:*:<UID>:role/<ROLE_MAPPING 里的角色1>",
        "acs:ram:*:<UID>:role/<ROLE_MAPPING 里的角色2>",
        "acs:ram:*:<UID>:role/<TEMP_AK_OSS_ROLE_ARN 的角色名>"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ram:GetUser",
        "ram:ListUsers",
        "ram:ListGroupsForUser"
      ],
      "Resource": "acs:ram:*:<UID>:user/*"
    },
    {
      "Effect": "Deny",
      "Action": [
        "ram:Create*",
        "ram:Update*",
        "ram:Delete*",
        "ram:Attach*",
        "ram:Detach*",
        "ram:Set*"
      ],
      "Resource": "*"
    }
  ]
}
```

> 当前线上 `TEMP_AK_STS_MAX_SECONDS=0` → 全走方案 B、不走 STS 分支
> （CLAUDE.md 明载）。那么第 1 条里的 `TEMP_AK_OSS_ROLE_ARN` 角色**现在就可以不列**，
> 等真要开 STS 分支时再加。少列一个宽 OSS 角色 = 少一个提权跳板。

### 2.4 第二主账号 `1949`

`ALIYUN_1949_ACCESS_KEY_*` 的用途是 `accounts.ram_client()` → 只走
`issuer._issue_ram` / `rewrite_ram_window` / `cleanup.revoke_grant`（`accounts.py:125-135` + 1.9e）。
所以它只需要 **2.2 的第 1/2/4/6 条**，且：

- user 前缀收到 `user/tempak-1949-*`（**连第 1 条也能收**——1949 档不建真人号）
- policy 前缀 `policy/temp-ak-auto-*`
- 不需要 `ram:AddUserToGroup`、不需要 `ram:SetVerificationInfo`、不需要 OSS/CMS
- 保留全部 Deny 兜底

这一把是三把里最容易做到真最小权限的，pretty 约 1.5KB。

### 2.5 字符上限与拆分

【文档】[RAM 配额限制](https://www.alibabacloud.com/help/zh/ram/product-overview/limits)：
**自定义策略内容上限 6144 字符；单个 RAM 用户最多附加 10 条自定义策略 + 20 条系统策略；
单条自定义策略最多 5 个版本。**

上面三条都在上限内（3791 / 4159 / 762，均以 pretty 计），现在不需要拆。
**真正会撑爆的是 2.1 第 4 条（OSS 按桶枚举）**：每个桶约 90 字符（两行 ARN），
pretty 3791 起步 → 大约 **28 个桶**就顶到 6144。
超了以后按这个顺序拆（每条独立、不互相依赖 Deny，因为 Deny 是跨策略生效的）：

1. `aiops-bot-ops-core` = PAI DSW + ECS + SLS + Prometheus + RAM 只读 + 两条 Deny
2. `aiops-bot-ops-oss` = 全部 OSS statement（桶清单放这里，独占 6144）
3. `aiops-bot-ops-transfer` = NAS + MGW + PassRole

拆成 3 条后一个 RAM 用户还剩 7 个自定义策略额度，够用。
**注意每条拆出来的策略都要各自带 Deny 兜底吗？** 不需要重复——阿里云的 Deny 在
「该用户所有生效策略的并集」上评估，任意一条里的 Deny 都会覆盖其它策略的 Allow。
但为了「单看一条策略也能看出边界」，建议至少把 2.1 的第 14 条（Deny RAM 写 + AssumeRole）
复制进每一条 ops 子策略，成本不到 200 字符。

---

## 3. 高危项清单

| # | Action | 为什么不得不给 | 风险 | 可用的收窄 / 建议 |
|---|---|---|---|---|
| H1 | `ram:CreateAccessKey` | 建号审批的「永久 AK」勾选 + 临时 AK 方案 B 发放（`ram_approval.py:445/618`、`issuer.py:161`） | **账号级提权原语**：建一个号 → 入一个宽组 → 建 AK，就拿到了组权限的长期凭证。而组由审批表单填、bot 只校验白名单 | ① 资源已限 `user/*` 同账号内；② 真正的闸门在 `ram:AddUserToGroup` 的组白名单（2.2 第 3 条）——**把它收到「最宽也只是只读组」**；③ 加 `acs:SourceIp` 限 bot 出口 IP（见 4.4）；④ 开 ActionTrail 对 `CreateAccessKey` 单独告警 |
| H2 | `ram:CreatePolicy` / `ram:CreatePolicyVersion` | oss_perm 下发 + temp_ak 时间窗 policy | **能写任意策略文档**。若前缀没限死，bot 可以造一条 `oss:*` 的策略再挂给自己建的号 | 已按 `policy/wuji-oss-auto-*` + `policy/temp-ak-auto-*` 收窄（前缀在代码里硬编码，`permsync.py:64`、`temp_ak/policy.py:21`）。**注意：前缀只限策略「名」，不限策略「内容」**——bot 仍可写出一条 `wuji-oss-auto-x` 内含 `"Action":"*"` 的策略。真正的兜底是 `AttachPolicyToUser` 的 Resource 只允许这两个前缀 + Deny 系统高危策略，**外加把 `AttachPolicyToUser` 的 user 侧也收窄**（见下） |
| H3 | `ram:AttachPolicyToUser` on `user/*` | 建号链要给人工填的登录名挂策略 | 配合 H2 = 完整提权链 | **建议进一步收窄 user 侧**：oss_perm 只挂给「飞书表格里的算法组成员」、temp_ak 只挂给 `tempak-*`。前者名单是动态的，没法写进 ARN；**但可以反过来 Deny**：`{"Effect":"Deny","Action":"ram:AttachPolicyToUser","Resource":"acs:ram:*:<UID>:user/<高权限管理员账号名>"}`，把几个 admin 子号点名排除 |
| H4 | `sts:AssumeRole` | 多租户隔离链的地基（`aliyun_sts._do_assume_role`） | `AliyunSTSAssumeRoleAccess` 是 `Resource:*` → **能 assume 账号里任意角色**，包括运维给自己建的 admin 角色。这比 RAM 写更隐蔽（AssumeRole 不留"改配置"痕迹） | **必须按角色 ARN 白名单**（2.3 第 1 条）。这是本报告里收益最大的单条改动 |
| H5 | `ram:DeleteUser` / `ram:DeleteAccessKey` | temp_ak 到期硬删 | 删错人 = 生产事故 | **已能收窄到 `user/tempak-*`**（全仓唯一调用点是 `cleanup.py`）。强烈建议照做 |
| H6 | `nas:CreateDataFlow` / `nas:DeleteDataFlow` | CPFS 没有现成绑定时临建临删 | **CreateDataFlow 会清空 Fileset**（CLAUDE.md 原话） | 单列 statement、只列智算版 `bmcpfs-*` fs-id（2.1 第 9 条）。更保守的做法：**完全不给**，改为要求运维预先建好 DataFlow 绑定，代码里 `resolve_dataflow` 找不到就报错 |
| H7 | `paidsw:DeleteInstance` / `ecs:DeleteInstance` / `ecs:RunInstances` | Agent 工具暴露的写动作 | 提示注入 → 删实例 / 开机器烧钱。`confirm=true` 由 LLM 自己填，**不是安全边界** | 建议：ECS 写动作**整条不给**（`manage_ecs` 在生产里基本没人用）；DSW `DeleteInstance` 保留但加 `acs:SourceIp` |
| H8 | `ram:SetVerificationInfo` | 绑安全手机/邮箱 | 资源不支持收窄，只能 `*` → 能改**任意** RAM 用户（含管理员）的安全手机 → 配合密码重置可接管账号 | 【文档】确认不支持资源级。缓解：**建议 dev 把这一步做成可开关**（`FEISHU_RAM_APPROVAL_SET_VERIFICATION=false` 时跳过），不用时就不给这条 Allow |
| H9 | `ram:PassRole`（若确需） | MGW 用 RAM role 访问 OSS | 能把任意角色传给服务 | 已限定到两个具体角色 ARN |

### 3.2 建议拆成第二个身份的部分

**已经在 2.1/2.2/2.3 拆了（3 个身份）。** 如果还要再拆一层，优先级是：

1. **`temp_ak` 发凭证独立成第 4 个身份**：它是唯一持有 `ram:Delete*` 的，
   且只碰 `tempak-*` 前缀。拆出去后 `aiops-bot-ram-write` 就彻底没有删除权。
   代码侧已经支持（`accounts.AccountProfile.ak_id/ak_secret` 是按档案注入的），
   只要给默认档也配一对专用 AK 即可 —— **但要先确认 `accounts.py:87` 那行不会被
   `permsync.make_ram_client()` 的零参路径绕过**（见 5.3）。
2. **Prometheus 读独立成第 5 个身份**：它是纯只读、且是**唯一需要 `cms:*` 的**，
   现在被硬塞进了 RAM 写那把 AK。拆出去零风险、收益是让 RAM 写 AK 的策略里少两条 `Resource:*`。

---

## 4. 现状对比与替换建议

### 4.1 `ALIYUN_ACCESS_KEY_*`（现：RAM 可写）

**能一把替掉吗？——能，但不是用一条策略，而是用 2.2 这条，且必须同时做两件事：**

1. 这把 AK 现在**不止**在做 RAM 写，还在做：
   - Prometheus Basic Auth（`tools/aliyun/prometheus.py:92`）——**全部监控/MFU/卡分布/巡检都断在这**
   - 九章迁移校验列源桶（`core/jiuzhang_transfer/verify.py:33-40`）
   - temp_ak 桶地域 + 存在性探测（`core/temp_ak_issuance/orchestrator.py:191`，AK 来自 `accounts.py:87`）
   - IMS `SetVerificationInfo`（`ram_approval.py:832` 的 fallback）
   所以 2.2 里第 8/9 条（OSS 只读 + CMS 只读）**不能删**，否则这把 AK 一换，早报和 MFU 卡直接全空。
2. `permsync.make_ram_client()` 的**零参路径优先读 `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET` 环境变量**
   （`permsync.py:364-365`）。如果部署环境设了这对 env，换 `ALIYUN_ACCESS_KEY_*` 是**没有效果的**
   （CLAUDE.md 已把这个坑记成硬规：绝不要设 `ALIBABA_CLOUD_*`）。
   `settings.print_validate()` 的告警行是客观依据（`settings.py:588-591`）。

### 4.2 `ALIYUN_BOT_MASTER_AK_*`（现：`AliyunSTSAssumeRoleAccess` + `RAMReadOnlyAccess`）

**必须保留这把独立 AK，但两条系统策略都该换成 2.3。**

- 保留理由：`assume_role_for_user` 是整条多租户隔离的唯一入口；
  一旦它和 RAM 写权限同居一把 AK，「STS 收窄」就没意义了（拿到那把 AK 就能自己建角色、自己 assume）。
- `AliyunSTSAssumeRoleAccess` 换成按角色 ARN 白名单（H4）。
- `RAMReadOnlyAccess` 换成 2.3 第 2 条的三个动作：代码只用到
  `ram:ListGroupsForUser`（`aliyun_sts.py:69`）+ `ram:GetUser` / `ram:ListUsers`（映射/查号回退路径）。
  `RAMReadOnlyAccess` 还包含 `ram:GetPolicy` / `ram:ListPolicies` 等，**能读到账号里所有策略文档**
  （含别人的自定义策略内容），对一个只需要查用户组的身份来说是不必要的信息暴露。
- ⚠️ 前提检查：CLAUDE.md 警告 `ALIYUN_BOT_MASTER_AK_*` 或 `ALIYUN_BOT_ROLE_MAPPING/DEFAULT`
  任一缺失 → `_do_assume_role` 恒返 None → `aliyun_client_factory._resolve_cred` **静默降级到
  `PAI_DSW_ACCESS_KEY_*` 全局 AK**（`:250-258`）。所以收窄 Master AK 后**必须看日志里没有
  `[ClientFactory] ... STS 失败，降级全局 AK`**，否则你以为收窄了，实际全走了 ops 那把。

### 4.3 `PAI_DSW_ACCESS_KEY_*`（任务书没提，但它才是主力）

`aliyun_client_factory._resolve_cred:253` 是所有云 client 的最后兜底，而后台任务
（调度器、对账线程、容量巡检、迁移编排）**都没有 open_id** → 全部落到这把 AK。
另外 `core/transfer/orchestrator.py:265` 还把它的 **AK/SK 明文交给火山 DMS**
作为「阿里 OSS 源凭证」（OSS→TOS 方向）——这意味着**这把 AK 的 Secret 会离开阿里云、进入火山的迁移服务**。

> 这是一条值得单独提的风险：如果启用 OSS→TOS 迁移，请给这条路径**换一把只读 OSS 的专用 AK**，
> 不要用 bot 的主力运维 AK。当前 `TRANSFER_*` 里已有 `TRANSFER_TOS_ACCESS_KEY` 的先例（反方向专用 AK），
> 建议对称地加一个 `TRANSFER_OSS_ACCESS_KEY`。

替换方案：挂 2.1 `aiops-bot-ops`。

### 4.4 建议叠加的 Condition

所有三条策略都建议加 `acs:SourceIp` 限制到 bot-server 出口 IP（`8.222.149.27`，
见 memory `deployment_server.md`）。写法（加在每个 Allow statement 里，或单独一条 Deny 更省字符）：

```json
{
  "Effect": "Deny",
  "Action": "*",
  "Resource": "*",
  "Condition": {
    "NotIpAddress": { "acs:SourceIp": ["8.222.149.27/32"] }
  }
}
```

一条约 150 字符，**杠杆最高的一条**：AK 即使泄漏，在别处也用不了。
⚠️ 上线前确认没有别的地方用同一把 AK（本地 CLI `python -m core.oss_perm...` 会被挡住），
以及容器的实际出网 IP（NAT/EIP 可能与登录 IP 不同）。

---

## 5. 代码侧建议（交 dev，researcher 不改代码）

1. **`tools/aliyun/prometheus.py:92`** 的 `_auth()` 改读一对独立的
   `PROMETHEUS_AK_ID/SECRET`（留空时回退 `ALIYUN_ACCESS_KEY_*` 保持兼容）。
   这一改让「RAM 写」那把 AK 的策略里能删掉 `cms:*` + `log:Query*` 的 `Resource:*` 条款。
2. **`core/jiuzhang_transfer/verify.py:33-36`** 改走 `utils.aliyun_client_factory.get_oss_bucket("")`
   （和 `core/ssh_transfer/verify.py` 一致），这样它用的是 ops 那把 AK，不再串味。
3. **`core/temp_ak_issuance/accounts.py:87`** 默认档的 `ak_id/ak_secret` 建议改成
   `TEMP_AK_RAM_ACCESS_KEY_ID/SECRET`（留空回退 `ALIYUN_ACCESS_KEY_*`），
   为「把发凭证拆成独立身份」留口子（3.2 第 1 条）。
4. **`core/transfer/orchestrator.py:265`** 的 `src_access_id=settings.PAI_DSW_ACCESS_KEY_ID`
   改用独立的 `TRANSFER_OSS_ACCESS_KEY`（4.3）。
5. 建议给 `ram_approval._set_verification_info` 加开关（H8）。

---

## 6. 火山引擎（优先级低）

`TOS_ACCESS_KEY/SECRET_KEY` 一把 AK 覆盖四个面（火山**没有 STS**，全是静态 AK）：

| 面 | 文件:行 | SDK / 方法 | 推断的 IAM Action |
|---|---|---|---|
| **TOS** | `tools/volcano/tos.py:32,47,107,284` | `tos.TosClientV2.list_objects_type2` | `tos:ListBucket` 【推测】 |
| | `tools/volcano/tos.py:85,93` | `get_object` | `tos:GetObject` 【推测】 |
| | `core/transfer/engine_tos.py:129` | `list_buckets`（桶→region 映射） | `tos:ListBuckets` 【推测】 |
| | `core/dataset_dashboard.py:164` | `list_objects_type2` | `tos:ListBucket` 【推测】 |
| **vePFS** | `core/vepfs_dataflow/engine_vepfs.py:142` | `VEPFSApi.create_data_flow_task` | `vepfs:CreateDataFlowTask` 【推测】 |
| | `engine_vepfs.py:163` | `describe_data_flow_tasks` | `vepfs:DescribeDataFlowTasks` 【推测】 |
| | `engine_vepfs.py:222` | `describe_file_systems` | `vepfs:DescribeFileSystems` 【推测】 |
| | `engine_vepfs.py:241` | `cancel_data_flow_task` | `vepfs:CancelDataFlowTask` 【推测】 |
| **DMS 迁移** | `core/transfer/engine_tos.py:108` | `DMSApi.create_data_migrate_task` | `dms:CreateDataMigrateTask` 【推测】 |
| | `core/transfer/engine_tos.py:147` | `query_data_migrate_task` | `dms:QueryDataMigrateTask` 【推测】 |
| **IAM 建号** | `core/ram_approval.py:549,555,580,589,601,618` | `IAMApi` get/create_user、create/update_login_profile、add_user_to_group、create_access_key | `iam:GetUser` / `CreateUser` / `CreateLoginProfile` / `UpdateLoginProfile` / `AddUserToGroup` / `CreateAccessKey` 【推测】 |
| | `core/volcano_iam_query.py:34,47,80` | list_access_keys / get_login_profile / get_user | `iam:ListAccessKeys` / `GetLoginProfile` / `GetUser` 【推测】 |
| | `core/temp_ak_issuance/issuer_volcano.py:49-95` | get/create_user、create/update_policy、attach_user_policy、create_access_key | `iam:*` 对应项 【推测】 |
| | `core/temp_ak_issuance/cleanup_volcano.py:22-65` | list/update/delete_access_key、detach_user_policy、delete_policy、delete_user | `iam:*` 对应项 【推测】 |

**全部标【推测】的原因**：火山文档站是 JS 渲染，`WebFetch` 抓不到正文（两次尝试均返回空），
SSH 进容器反查 SDK 也被权限拦下。**建议的取证路径**（交 dev 或下一轮 researcher）：

1. 在 `aiops-bot` 容器里 `python -c "import volcenginesdkvepfs as v; print([x for x in dir(v) if 'Request' in x])"`
   拿到精确的 Request 类名 → 对应 Action 名。
2. 火山「访问控制 → 策略 → 新建自定义策略」的控制台**动作选择器**会列出该产品全部 Action 字符串，
   截图即可（只读操作，不保存策略）。这是最快的路。
3. 火山系统预设策略 `vePFSFullAccess` / `TOSFullAccess` / `IAMFullAccess` 的策略详情页也能看到动作清单。

**过渡建议**：火山侧短期先用系统策略组合
`TOSReadOnlyAccess` + `vePFSFullAccess` + `IAMFullAccess` + DMS 相关，
**并且把 `TOS_ACCESS_KEY` 拆成至少两把**：
- 一把只读 TOS + vePFS + DMS（容量巡检 / 预热沉降 / 迁移）
- 一把 IAM 写（建号 + 发凭证）

理由与阿里侧同：现在这一把 AK 同时能读全部数据**和**建 IAM 用户 + 发 AK，
泄漏一次等于把火山账号整个交出去。这是**比阿里侧更紧急**的问题，因为火山没有 STS、
没有临时凭证、这把 AK 是长期有效的，且 `TOS_ACCESS_KEY` 在 `.env` 里明文。
（`.env` 里已确认存在 `TOS_ACCESS_KEY` / `TOS_SECRET_KEY` 键名；未读取其值。）

---

## 7. 未核实 / 需真机取证的清单

| # | 项 | 现状 | 取证路径 |
|---|---|---|---|
| U1 | `ram:PassRole` 是否为 MGW 指定 role 时的必需权限 | 【推测】文档准备页未明说 | 先按给的发，若不需要再删；或用 RAM 策略模拟器试 |
| U2 | 2.2 第 12 条 Deny 的资源匹配语义（会不会误伤 `ram:ListUsers`） | 【推测】 | RAM 控制台「策略模拟」；或拿一个测试子号先挂上跑一遍建号链 |
| U3 | MGW Action 的 `Import` 中缀名是否适用于 `2024-06-26` 版本 | 【文档】但页面是 JS 渲染后抓的，两次抓取一致 | 用 `MGW_USER_ID` + 一个**不存在的**地址名调 `get_job`，看 403 报文里回显的 Action 名 |
| U4 | 火山全部 Action 名 | 【推测】 | 见第 6 节三条路径 |
| U5 | 线上 `.env` 实际配了哪几把 AK、`CPFS_FILE_SYSTEM_IDS` 里有几个 fs、`CAPACITY_MONITOR_TARGETS` 里有几个桶（决定 2.1 第 4/8 条要列多少 ARN） | 未取 | SSH 到 bot-server 只读 `cut -d= -f1`/`grep` —— **本轮被权限拦下**，需用户放行或 dev 代取 |
| U6 | bot 自己的 RAM 用户名（2.2 第 12 条的占位符） | 未取 | 同 U5 |
| U7 | 容器出网 IP（4.4 的 `acs:SourceIp`） | 未取 | 容器内 `curl -s ifconfig.me` |
| U8 | `oss:GetBucketInfo` 是否为 `get_bucket_location` 的必需伴生权限 | 【推测】代码只调 `get_bucket_location()` | 保守起见两个都给了；实测可只留 `oss:GetBucketLocation` |

---

## 出处

- [RAM 配额限制（6144 字符 / 10 条自定义策略 / 5 版本）](https://www.alibabacloud.com/help/zh/ram/product-overview/limits)
- [RAM 授权信息（Action ↔ 资源 ARN）](https://help.aliyun.com/zh/ram/developer-reference/api-ram-2015-05-01-ram)
- [IMS SetVerificationInfo 授权信息（`ram:SetVerificationInfo`）](https://help.aliyun.com/zh/ram/developer-reference/api-ims-2019-08-15-setverificationinfo)
- [PAI DSW ListInstances 授权信息](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-listinstances)
- [PAI DSW GetInstance 授权信息](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-getinstance)
- [PAI 基于 RAM Condition 的资源访问授权](https://help.aliyun.com/zh/pai/set-access-permissions-for-resources-based-on-the-condition-attribute-of-ram)
- [在线迁移服务 RAM 权限策略（mgw Action 清单 + 资源 ARN）](https://help.aliyun.com/zh/data-online-migration/permission-policy)
- [在线迁移服务 OSS 桶间迁移准备工作（信任主体 mgw.aliyuncs.com）](https://help.aliyun.com/zh/data-online-migration/user-guide/preparations-for-migration-between-oss-buckets)
- [hcs-mgw-20240626 Go SDK 方法清单（印证 API 操作名）](https://pkg.go.dev/github.com/alibabacloud-go/hcs-mgw-20240626/client)
- [NAS CreateDataFlowTask 授权信息（`nas:CreateDataFlowTask` + filesystem ARN）](https://help.aliyun.com/zh/cpfs/cpfsonecs/developer-reference/api-nas-2017-06-26-createdataflowtask-cpfs)
- [NAS 授权信息总表](https://www.alibabacloud.com/help/zh/nas/developer-reference/api-nas-2017-06-26-ram)
- [ARMS Prometheus HTTP API（AK Basic Auth + 所需系统策略）](https://www.alibabacloud.com/help/zh/arms/prometheus-monitoring/http-api-urls)
- [AliyunPrometheusMetricReadAccess 策略内容](https://help.aliyun.com/zh/ram/developer-reference/aliyunprometheusmetricreadaccess)
- [SLS RAM 自定义授权场景与策略](https://help.aliyun.com/zh/sls/use-custom-policies-to-grant-permissions-to-a-ram-user)
- 内部参考：`/home/l/桌面/infra/deploy/panel/executor-policy.aliyun.example.json`（Deny 兜底写法来源）

# 多区域边界与演进方案

相关：[架构](architecture.md)｜[CI/CD](cicd.md)｜[运维](runbook.md)

## 当前结论

当前实现是**单区域部署模板**，已验证区域为 `cn-hangzhou`，不能在简历或交付说明中
写成“已支持多区域”。`var.region` 只是在一个 state 内选择区域，不等于多区域：

- `dev/platform.tfstate` 与 `dev/access.tfstate` 每个环境只有一份；
- RAM 角色名是账号级全局名称，第二个区域会重名；
- PAI Workspace、CPFS、vSwitch、挂载点、runner 和训练算力都有区域/可用区边界；
- CPFS 不能跨可用区挂载，更不能跨区域挂载；
- 当前 workflow matrix 只有 env/layer，没有 region。

因此现在强行把 `region` 改成列表会让多个区域共享 state、审批和故障域，是错误实现。

## 正确的目标拆分

```text
账号全局（一个）
├── bootstrap：state 后端、OIDC Provider、Terraform 角色
├── access-global：RAM 策略/角色，聚合所有区域资源范围
└── platform-global：lakeFS/归档 OSS 的主存储与复制策略

环境 × 区域（每区独立 state）
└── regional-runtime
    ├── PAI Workspace 成员
    ├── CPFS 引用、Fileset、DataFlow、协议服务/挂载点
    ├── 同区 runner/DSW/DLC 资源约束
    └── 区域级 PAI Dataset 与发布配置
```

建议 state key：

```text
terraform/global/bootstrap.tfstate
terraform/<env>/global/access.tfstate
terraform/<env>/global/platform.tfstate
terraform/<env>/regions/<region>/runtime.tfstate
```

每个区域独立 plan artifact、Environment 审批与 concurrency group。一个区域 apply 失败
不应锁住或污染另一区域 state。

## 数据版本如何跨区域

Commit ID、manifest 与 Paimon Snapshot 是全局身份；CPFS release 是区域热副本。训练始终
使用相同 Commit，但每个区域分别沉降并注册自己的 PAI Dataset Version：

```text
lakeFS Commit C
├── cn-hangzhou CPFS /datasets/<dataset>/C/ → PAI Version H
└── cn-shanghai CPFS /datasets/<dataset>/C/ → PAI Version S
```

不能让上海训练跨区挂杭州 CPFS。对象字节应先通过受管的 OSS 跨区域复制到目标区域，
再从目标区域预热；复制完成与 manifest 校验通过前，不得注册目标区域的 PAI Version。
lakeFS 的物理地址映射也必须纳入设计，不能只复制对象却假设原 Commit 自动指向副本。

## 实施顺序

1. 先在不移动现有 state 的前提下抽出 `regional-runtime` 模块，并在杭州做无变更 plan；
2. 用流水线审批的 `terraform state mv`/import 迁移现有 state，禁止本地操作；
3. 给全局 RAM 角色增加区域资源集合，角色名保持唯一；
4. workflow matrix 增加 region，artifact、state key、Environment 与 concurrency 都带 region；
5. 在第二区域只建空环境，先验 CPFS/挂载点/算力库存交集；
6. 做一个小 Commit 的复制、沉降、PAI RO 挂载和训练门禁验收；
7. 验收通过后才开放正式数据集。

第二区域、复制策略与 state 迁移尚未获批，因此本仓库本次只固化边界和实施方案，
不伪造“多区域已完成”，也不在本地执行任何 state 操作。

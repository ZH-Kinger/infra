# 九章往来链路

状态：面板第一阶段已接入（2026-09-30）。九章本地落点已按真机目录校准。

九章不是第二朵云，也没有可以让面板提交任务的迁移 API。面板通过固定 host key
的 SSH 连接到九章，在九章机器上调用 `ossutil`，因此链路只有两种方向：

```text
oss://wuji-bucket-hangzhou/<稳定路径>/ ──直接拉取──> jz://jz-b200/wuji-data-tran/<平台>/<数据类型>/<批次+版本>/
jz://jz-b200/<结果路径>/ ──回传──> oss://wuji-data-tran/alayanew/<数据类型>/...
```

第一段不经过新加坡，也不经过新的中转桶。九章本地已有三个其它平台落点：

```text
/root/nas/wuji-data-tran/
├── aliyun/<数据类型>/<批次+版本>/
├── volcano/<数据类型>/<批次+版本>/
└── turboai/<数据类型>/<批次+版本>/
```

回传段使用已经定稿的 `wuji-data-tran/alayanew/` 命名空间，第二层仍然使用数据类型
词表，例如 `alayanew/third-party-data/<供应商>/<批次ID>/`、
`alayanew/internet/<来源>/<批次ID>/` 和 `alayanew/teleop/<设备或场地>/<批次ID>/`。
九章回来的 teleop 数据落到 `alayanew/teleop/`。

## 面板入口

统一的「数据迁移」申请仍然只有两个路径。九章路径使用：

```text
jz://jz-b200/wuji-data-tran/<平台>/<数据类型>/<批次+版本>/
```

提交前会根据方向显示链路，并在服务端再次校验。`jz://` 只接受模板中登记的
`jz-b200`，不能任意写主机或任意远端路径；平台只能是 `aliyun`、`volcano`、`turboai`。
九章内部目录之间不提供面板直连。

## 运行配置

`delivery-moves.service` 运行用户只读取以下配置：`JIUZHANG_HOST`、`JIUZHANG_PORT`、
`JIUZHANG_USER`、`JIUZHANG_SSH_KEY_FILE`、`JIUZHANG_HOST_KEY`，以及可选的
`JIUZHANG_DEST_ROOT`、`JIUZHANG_WORK_DIR`、`JIUZHANG_OSS_ENDPOINT`、并发参数。

私钥放在面板服务器上的独立文件，权限只能是 0600 或 0400；不放进申请单、JSON
台账或普通日志。每张单单独签发阿里云前缀凭证：去程只有源目录 list/read，回程
只有 `alayanew` 目标目录 list/write。AK/SK 通过 SSH/SFTP 写到九章的 0600 临时
`ossutil` 配置，不出现在命令行；任务结束由远端 `EXIT` 清理并由面板再次兜底删除，
同时撤销 RAM 子账号。没有 host key、私钥权限过宽或连接失败时任务保持失败并提醒，
不会降级成不校验的连接。

九章只允许一张传输单在运行。远端用 `flock` 持有主机级锁，第二张单在凭证写入后
会立即清理并返回“已有传输任务”，不会并行启动；面板调度器也不会把它当成已提交。

## 还需要上线前完成

1. 在九章机器上确认专用迁移账号（当前代码兼容现有 root 账号，但生产建议改成最小权限账号），并确认该账号能访问目标 GPFS 目录、安装 `ossutil`，以及允许面板账号通过 SFTP 写入 `/tmp` 临时配置。
2. 把九章 SSH 公钥写入面板服务器的 `JIUZHANG_HOST_KEY`，把私钥文件交给 `delivery` 用户并设为 0600/0400。
3. 用一个小测试目录跑去程和回程，确认对象数、字节数和抽样内容，再开启 `delivery-moves.timer`。
4. 回传校验与稳定桶沉降仍是第二阶段；在此之前，面板只写 `alayanew/` 回传命名空间，
不会把九章结果直接写入主数据桶。

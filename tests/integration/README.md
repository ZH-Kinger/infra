# 真实环境集成测试

这些测试只读阿里云控制面，默认 skip；`make test` 仍然完全离线。凭证使用 aliyun CLI
默认凭证链或 `INTEGRATION_ALIYUN_PROFILE`，不要把 AccessKey 写进环境文件或仓库。

现有 dev 环境可这样验证：

```bash
INTEGRATION_ALIYUN_REGION=cn-hangzhou \
INTEGRATION_PAI_WORKSPACE_ID=617398 \
INTEGRATION_CPFS_FILESYSTEM_ID=cpfs-00a27a8ec8b1e13a \
python3 -m unittest tests.integration.test_cloud_readonly -v
```

测试只调用 PAI 的 List/Get 与 NAS 的 Describe API，不提交 DLC/DSW 作业，
也不创建 DataFlowTask。

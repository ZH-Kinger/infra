# OSS / TOS 桶现状实测（2026-09-21）

> **这是附录，不是规范。** 规范见 `oss-storage-spec.md`。
> 这份留着的是 9/21 只读实测的数据：各桶容量、对象数、版本控制、生命周期、一二层目录。
> 其中「第五节以后」是规范的早期草稿，**已被 oss-storage-spec.md 取代**，保留只为留痕。
> 之后的变化：wuji-egocentric-processed 已决定删除；wuji-provider-hz / wuji-processed-hz /
> wuji-processed-sing 已于 2026-09-22 新建。

# OSS / TOS 存储规范

状态：**待评审**。
桶清单与 1～2 层目录：2026-09-21 只读实测（阿里云 OSS 主账号 `1704065796538912` 26 个桶、
火山 TOS 账号 `2111674479` 17 个桶）。容量、对象数、版本控制、生命周期：同日
`GetBucketStat` / `GetBucketVersioning` / `GetBucketLifecycle` 实测。
标「沿用旧版」的，是上一轮定下来、今天没有重新取证的部分。

**两条总纲，本规范任何一条都不能违反：**

1. **只新建一个桶。** 现网桶已经覆盖所有角色，缺的是「这个桶是干什么的」写下来。
   唯一的例外是收货桶 `wuji-inbox-hz`（第五节）—— 它存在的理由不是「需要一个地方放」，
   是**权限边界**：供应商要能写，而主本一个字节都不能给外部写。这件事目录解决不了，
   `oss:Prefix` 能限制写到哪个前缀，却挡不住桶内其余前缀的读和列举。
2. **不挪存量数据。** 规范只管新数据往哪落。大目录尽量连名字都不改 ——
   搬一次是成本、是风险（谁在引用它没人说得清），收益只是好看。

规范只约束**第一层和第二层目录**。第三层往下（批次内部的分片、文件名）由各自流程决定，
本规范不管，只要求批次那一层的命名符合第六节的字符集。

**不预先建占位目录。** OSS 没有真目录 —— 没用到的一级目录就是不存在，不占空间、不计费、
不在列表里碍眼。所以第五节那份词表是**规则，不是要去建的目录结构**；用到才出现。

---

## 一、今天的真实状态

### 阿里云 OSS，13 个自管桶

| 桶 | 地域 | 容量 | 对象数 | 版本控制 | 旧版本清理 |
|---|---|---|---|---|---|
| `wuji-bucket-hangzhou` | 杭州 | **859.7 TiB** | 67,589,201 | 开 | **没有**（只有 `oss-accesslog/` 一条） |
| `wuji-sing` | 新加坡 | 75.6 TiB | 15,136,594 | 开 | 3 天 |
| `wuji-egocentric-processed` | 杭州 | 32.4 TiB | 15,118,338 | 开 | 3 天 |
| `wuji-data-tran` | 杭州 | 23.9 TiB | 3,152,921 | 开 | 3 天 |
| `wuji-bangkok` | 曼谷 | 13.6 TiB | 2,839,664 | 开 | 3 天 |
| `wuji-datasets-hz-6c661af0` | 杭州 | 13.4 TiB | 356,010 | 开 | 3 天 |
| `ai-prod-wj-wl-oss` | 乌兰察布 | 1.93 TiB | 5,592 | 开 | 3 天 |
| `rl-data` | 呼和浩特 | 1.10 TiB | 1,572,227 | 开 | 3 天 |
| `wuji-rl-dataset` | 杭州 | 804.5 GiB | 327,195 | 开 | 3 天 |
| `wuji-test-data` | 北京 | 39.7 GiB | 5,689 | 开 | 3 天 |
| `data-factorys` | 深圳 | 2.99 GiB | 101 | 开 | 3 天 |
| `wuji-algo-dev-hz` | 杭州 | 0 B | 150 | 开 | 3 天 |
| `wuji-algo-dev-sing` | 新加坡 | 0 B | 13 | 开 | 3 天 |

两个开发桶 0 字节 / 150 个对象，里面全是目录占位符 —— **建好了但还没人用**。

**`wuji-bucket-hangzhou` 是唯一一个开了版本控制却没配旧版本清理的桶，而它是最大的那个。**
上一轮在两个开发桶上发现过这个状态并修掉了，主桶漏了。后果具体：6759 万个对象里任何一次
覆盖写都会留一个旧版本，永久计费，且 `ls` 看不见 —— 账单只涨不落，没有任何界面会告诉你
钱花在哪。**这是本次最值钱的一条发现，配一条 `NoncurrentVersionExpiration` 就能止血。**

### 火山 TOS，8 个自管桶

按 1～2 层目录判断（没读文件内容）：

| 桶 | 地域 | 是什么 |
|---|---|---|
| `wuji-dc-shanghai` | 上海 | 数采落盘，113 个顶层目录，绝大多数是 `YYYYMMDD`（`20260527`～`20260921`，今天还在写） |
| `wuji-egocentric-data` | 上海 | ego 数据 + 开源数据集（`ego4d` `epic-kitchen` `open-x-embodiment` `VITRA-1M` `dex-wild`） |
| `wuji-ego-processed` | 上海 | `wuji-egocentric-processed`（阿里）的跨云镜像，目录一一对应 |
| `data-sync-b2d` | 上海 | 同步中转，17 个顶层目录，一半是人名 |
| `data-tran` | 上海 | 跨云迁移 staging（bot 的 `PFS_STAGING_MAP` 用它） |
| `ego-output` | 上海 | `epic-kitchen-cleaned/` + `test/` |
| `eog-pretrain-code` | 上海 | 代码/依赖，3 个目录，其中一个**名字是空串**（key 就叫 `/`） |
| `umi-tos-raw` / `-lerobot` / `-lance` / `-internet` / `-internet-lance` | 广州 | UMI 流水线 raw→lerobot→lance；两个 `internet*` **今天是空的** |
| `data-infra-sha` | 上海 | `ray-entrypoint/` + `tmp/`，基础设施用 |

**注意名字重合**：阿里云的 `wuji-ego-processed` 今天已删（打错名字的空桶，与
`wuji-egocentric-processed` 撞名）。**火山 TOS 上同名的 `wuji-ego-processed` 是另一个桶，
有数据，不要跟着删。** 两个名字只差一个词，删错一次就是跨云镜像整份没了。

---

## 二、不归本规范管的桶

这些是云产品自己开的，或归别的系统管。**别往里放数据**，也别按本规范去动它们的目录。

**阿里云（13 个）**

```
cri-35v79obidmymmwfn-registry              乌兰察布   ACR 镜像仓库
cri-9c1k8plx3wm4eiki-registry              北京        同上
cri-9j67oat8c3scne73-registry              杭州        同上
cri-9jqag16i23iea9de-registry              呼和浩特    同上
cri-eisfhaf2dev95s0o-registry              杭州        同上
cri-h9xrulcetyaelt67-registry              深圳        同上
cri-n3w4t3l9ud0ld1dk-registry              呼和浩特    同上
cri-u988yjnqyp653fde-registry              新加坡      同上
cri-wvx8qqq3308hs5pv-registry              呼和浩特    同上
cri-xeeh70n3nxyft7sc-registry              呼和浩特    同上
h2r-dlc-1704065796538912-cn-shanghai       上海        PAI DLC 自建输出桶
oss-pai-w49zcds9py18fjthte-ap-southeast-7  泰国        PAI 自建，今天是空的
data-infra-emr-log-hgh                     杭州        EMR 集群依赖桶
```

**火山（4 个）**

```
ml-platform-auto-created-required-2111674479-cn-beijing     机器学习平台自建，空
ml-platform-auto-created-required-2111674479-cn-guangzhou   同上，空
ml-platform-auto-created-required-2111674479-cn-shanghai    同上，**已被当成个人空间用**
las-datastore                                               LAS 数据服务自建
```

两个例外要单说，因为它们**名字是服务桶，内容不是**：

- **`data-infra-emr-log-hgh` 名字像 EMR 日志，实际装着团队编译产物**
  （`compiled/` `thirdparty/` `model/` `models/` `results/` `videos/`），10.9 GiB。
  EMR 那边要它，删不得；同时它和 `wuji-test-data` 内容高度重合。
  **不搬，只登记**，新的编译产物一律进 `wuji-test-data/artifact/`。
- **`ml-platform-auto-created-required-...-cn-shanghai` 被当成个人空间在用**
  （`chenxinyi/` `handuo/` `qiaodongming/` … 20 个人名目录）。这是平台自动建的桶，
  **平台有权按自己的逻辑清理它** —— 把数据放这儿等于存在一个自己没有处置权的地方。
  要迁各人自己迁到开发区，规范不强制。

---

## 三、全部桶的 tree

只到第二层。

### 阿里云 OSS · 数据桶

```
wuji-bucket-hangzhou/                    杭州 · 859.7 TiB · 6759 万对象 · 挂载为 /oss
├── third-party-data/                    51 个二级目录，六类内容全平铺在这一层
│   ├── Haiyu/ jingdong/ lightwheel/ lingchu/ lingsheng/
│   │   maxinsights/ nuoyiteng/ shutu/ zhiyuan/        供应商交付
│   ├── Haiyu_trans/ nuoyiteng_lerobotv3/ nuoyiteng_lerobotv3_21/   格式转换输出
│   ├── real-world-data/                 自采（ego_0729/ egodata_0728/）—— 不是第三方
│   ├── label/                           自己打的标（Haiyu_trans/ lightwheel/ shutu/ worldengine/）
│   ├── zijie_youtube/ volcano/          互联网抓取
│   ├── adt/ aether/ agibot-world-2025/ allenai_so100101/ bc_z/
│   │   behavior_1k/ bimanual_yam/ bridgedata_v2/ droid/ egodex/
│   │   egosuite/ egoverse/ fractal/ kuka/ libero/ rh20t/ robocasa/
│   │   nvidia_gr1_teleop/ nvidia_groot_xembodiment_sim/
│   │   OpenAoE/ PalmDex/ RekaDaily-10k-raw/           开源数据集（约 24 个）
│   ├── egoscale/ ego_10h_videos/ worldengine/ xspark-dexbench/
│   │   Lightwheel_2000h_0713/           自产/混合，来源待认领
│   ├── v1.0/ vl/ w0/ w0-multimodal/ rock-climb-pilot/  **无主**
│   ├── jz-smoke-lakefs/ .cache/         测试与缓存残留
│   └── " shutu/"                        **目录名带前导空格**，见第七节
├── public-datasets/                     **空的**（只有一个占位对象）
├── opensource_dataset/                  SynData/ egodex/ egoverse/ larybench/ .claude/
├── third-party-action/                  lightwheel/ worldengine/          重定向产出
├── third-party-data-labelv1/            label/ processed/                 旧标注路径
├── label/                               Haiyu_trans/ nuoyiteng_lerobotv3_21/ real-world-ego/
│                                        shutu/ vitra-1m/ worldengine/
├── teleop/                              lerobotv3_21x/ lerobotv3_4xx/ lerobotv3_7xx/
│                                        openwam-data-efficiency-*/ zhangyl/
├── worldmodel/                          AgiBotWorld2026/ Galaxea-Open-World-Dataset/
│                                        RoboCoin/ RoboMIND/ RoboMIND2.0/ openx_lerobot/
│                                        lerobot3/ jiaqiliang/ scripts/ ._____temp/
├── mask/                                worldengine/ qc_standard-e5d96504/
│                                        legacy-pre20260908/ _thresholds/
├── egoscale/                            datasets/ models/ processed/ RD/ .local/ pip_cache/
├── egoscale_dataacceptance/             Image_data_diversity/
├── ego_pretrain/                        VITRA/ VITRA_for_test/ lerobot/ huggingface/
│                                        aliyun_import_report/ .vscode/ "/"
├── embodied-data-dev/                   data/ _lakefs/
├── lakefs/                              w0-dataops/
├── datamill/                            drill-m3/ rel2acc/
├── rel2acc/                             we3000-finelabel/
├── w0/                                  code/ chenrankou_ablation/ jizx-starvla-parity/
│                                        state-only-*-eval-r1..r4/
├── w0_validation/                       api/ resources/ results/ tmp/
├── w0xfer/                              openwam-retarget-20260915/
├── wuji-il/                             wuji-hand-teleop-data/ wuji-sim-data/
├── wuji-hontai-tactile/                 Dexonomy/ dexonomy_archive/ tactile-*/
│                                        00_random_force_raw_data*/
├── wuji-dc-beijing/                     1/ 2/ 3/ 5/ .claude/
├── wuji-data-driven-caliber/            wuji-data-driven-caliber/   （嵌套重了一层）
├── wuji_openpi/                         code/ data/ .cache/ aliyun_import_report/
├── wuji-sft-data/                       **空的**
├── lerobot_full_data/                   单个 35.7 GiB tar，无目录
├── research/                            guanqihe/
├── research-artifacts/                  shutu-vlm-probe/ we-pretraining-scale-efficiency/
├── model/                               we-q08-fla-1000h-frz-v1/ we-q08-fla-500h-frz-v4/
├── checkpoints/                         zhangyl/
├── codex/                               openwam-starvla/
├── share/                               model/
├── oss/                                 worldmodel/
├── cross-cloud/ cpfs-to-vepfs/ vepfs-to-cpfs/   搬运链中转
│                                        （zhuzijie/ ckpt/ model_train/ retransfer/）
├── staged/                              **空的**
├── oss-accesslog/                       1507 个日志对象（唯一配了生命周期的前缀）
├── .backup_drop20_20260811/             20260806_tianji1_7.1.207｜208｜264/
└── caomaosong/ chenxinyi/ guanqihe/ pengjw/ zexianji/ zhangyl/
                                         **个人目录混在数据桶里**

wuji-egocentric-processed/               杭州 · 32.4 TiB · 1512 万对象
├── raw/                                 **空的**（只有占位对象）
├── extracted/       egoverse/
├── processed/       filtered/ reprocessed/
├── label/           task_split/ time_split/
├── result/          epic-kitchen/ task_split/
├── results/         epic-kitchen/       ← 和 result/ 重名，差一个 s
└── scripts/         egoverse/

wuji-datasets-hz-6c661af0/               杭州 · 13.4 TiB · 35.6 万对象
├── worldengine-224p-gop1-rg256-260813/  121 个 shard（20260713_1300hr_shard_000 …）
├── climbing_datasets/                   HMDB51/
├── openwam_data_efficiency_20260902/    assets/
├── xspark_dexbench_spark0/              v1-20260820/
└── .dlsdata/                            .sysinfo/    ← PAI 数据集服务自己写的

data-factorys/                           深圳 · 2.99 GiB · 101 对象 · 数据工厂专用
└── test/                                20260723/（ros2_bags/ ros2_bags_clean/）+ .mcap_studio/

wuji-test-data/                          北京 · 39.7 GiB
├── gmt-ckpt/ model/ models/             训练检查点、权重
├── compiled/ thirdparty/ wheels/        编译产物、依赖缓存
├── results/ videos/                     训练结果、可视化
├── ant/ jobs/                           代码包、任务记录
└── w-cf250582045c421a/                  EMR/DLC 工作区自建

ai-prod-wj-wl-oss/                       乌兰察布 · 1.93 TiB · 产线
└── ali_ppu_test/                        OpenWAM-main/ VITRA_for_test/
```

### 阿里云 OSS · 开发区与中转

```
wuji-algo-dev-hz/                        杭州 · 0 B · 150 个占位对象
├── algo/          heguanqi/ jake/
├── data-collect/  liyuehui/ yubohua/ zhuliang/
├── data-sys/      hubohua/ wangchunlin/ zhangzichao/
├── general/       leyang/ liyi/ qiaodongming/ wangyuran/ wangzihan/ xinyue/ yuanzhen/
├── imitation/     huangsiqiao/ jizexian/ kouchenran/ liangjiaqi/ pengjingwei/
│                  qianbinsheng/ wangyizhou/ zhangwentao/ zhangxiaoxiong/
│                  zhangyulin/ zhuzijie/
├── inference/     lianqiuyou/
├── posttrain/     chenxinyi/ huangchaogui/ yaoshenzhe/ yujichuan/ zhangxilin/
├── pretrain/      caomaosong/ chuzhong/ guoyubo/ yuxianggang/ zhouziyue/
├── proprio/       chenjiahao/ wubo/
└── rl/            jiangxiangrui/ jinxuhao/ lichengmeng/ liuxiaohan/ liyayan/
                   shenglijie/ wujielin/ wuliqi/ yanghan/ zengzhaoqing/

wuji-algo-dev-sing/                      新加坡 · 0 B · 13 个占位对象
├── algo/          heguanqi/ jake/
├── imitation/     huangsiqiao/ jizexian/ kouchenran/ pengjingwei/
│                  qianbinsheng/ wangyizhou/ zhangwentao/
├── posttrain/     huangchaogui/
└── pretrain/      chuzhong/ yuxianggang/ zhouziyue/

wuji-data-tran/                          杭州 · 23.9 TiB · 中转 + PFS staging
├── chenrankou/ chuzhong/ guanqihe/ qianbsh/ wangyuran/
│   xiaoxiong/ yuboguo/ zexianji/ zhangchenhao/     **人名目录混在中转桶里**
├── lightwheel/ vitra/ data_conform/ dataset_test/
├── vitra_sft_wuji_mask54d_stitched_headtop_full_ft_20260608_0616/
└── ossutil_output/

wuji-sing/                               新加坡 · 75.6 TiB · SSH 迁移链段1落点/段2源
├── third-party-data/ egoscale/ worldengine/ worldmodel/ w0/ vitra/
├── PalmDex/ RekaDaily-10k-raw/ pretrain_processed_data/ models/
├── guanqihe/ wangyizhou/ wangyuran/ xiaoxiong/      人名目录
└── data-line-test/ openwam-miles-test/ ossutil_output/

wuji-bangkok/                            曼谷 · 13.6 TiB · 泰国 H200 就近副本
└── chenrankou/ lightwheel/ vitra/ wangyuran/ zhangchenhao/
```

### 阿里云 OSS · 自带版本机制、只登记不改

```
rl-data/                                 呼和浩特 · 1.10 TiB · 157 万对象
├── lakefs/ _lakefs/                     lakeFS 数据版本系统
├── forge_releases/ forge_release_history/ forge_release_backups/ forge_staging/
├── releases/ backups/ raw_datasets/ data/ public/ model/ gmt/
└── codex_staging/ tmp/

wuji-rl-dataset/                         杭州 · 804.5 GiB
├── forge_releases/ forge_staging/ backups/
├── codex-transfer/ codex_staging/
├── datasets/ raw_datasets/ dma-verified-data/ gmt/ tmp/
└── uploadFile.txt                       （桶根散落文件）
```

### 火山 TOS

```
wuji-dc-shanghai/                        上海 · 数采落盘
├── 20260527/ … 20260921/                97 个日期目录，今天仍在写
├── trans/ trans_bak/ trans_backup/ trans_for_encode/ Trans_deploy/
│   tianji2_trans_wrist_cam/             转换产出，六套并存
├── "lerobot v3/"                        **目录名带空格**，见第七节
├── qc_previews/ dagger/ hf_data/ rlt_models/ vitra-dc/
├── jichuan/ wangchunlin/ xiaoxiong/     人名目录
└── .teleop_convert_locks/

wuji-egocentric-data/                    上海 · ego + 开源数据集
├── ego4d/ epic-kitchen/ epic-kitchen-processed/ open-x-embodiment/
│   VITRA-1M/ dex-wild/ egocentric/ egocentric_tar/ egoscale/
├── third-party-data/ wuji/ test/
├── "opic-titchen/"                      **epic-kitchen 的拼写错误目录**
└── " egocentric--processed/"            **带前导空格 + 双横线**

wuji-ego-processed/                      上海 · wuji-egocentric-processed 的跨云镜像
└── raw/ extracted/ processed/ label/ result/ scripts/
    cpfs-to-vepfs/ vepfs-to-cpfs/ cross-cloud/ pipeline_smoke/

data-sync-b2d/                           上海 · 同步中转
├── human_pick_place/ human_robot_paired_datasets/ mls-bench-data/
│   robotwin_2_0/ proj-matrix/ wuji-il/ wuji-hand-teleop-data/
├── handuo/ huangsiqiao/ lzicong/ pengjw/ qianbsh/ zhangwt/ zhujl/ zhuzijie/
├── outputs/ tmp/
└── bandwidth_test                       512 MiB 测速文件留在桶根

data-tran/                               上海 · 跨云迁移 staging（PFS_STAGING_MAP）
└── wuji-il/ wuji_openpi/ + 两个 dms 自测报告文件

ego-output/                              上海
└── epic-kitchen-cleaned/ test/

eog-pretrain-code/                       上海
└── VITRA_for_test/ huggingface/ "/"     ← 有一个名字为空串的目录

umi-tos-raw/                             广州 · UMI 原始
└── UM12A00260221001/ UM12A00260311999/ handuo/ wentao/ zhujl/ zijie/
umi-tos-lerobot/                         广州 · UMI → lerobot
└── 20260302_144808_v5/ 20260302_144808_v6/ handuo/ zhujl/
umi-tos-lance/                           广州 · UMI → lance
└── raw/ clean/ enhance/ ssv2/
umi-tos-internet/ umi-tos-internet-lance/  广州 · **今天都是空的**

data-infra-sha/                          上海 · 基础设施
└── ray-entrypoint/ tmp/
```

---

## 四、分档：按「丢了能不能再拿到」

沿用旧版。这条轴决定备份强度、版本保留、谁能删，**不是按业务名分**。

| 档 | 内容 | 旧版本保留 | 数据本身 | 谁能删 |
|---|---|---|---|---|
| **A 拿不回来** | 自采、供应商交付、标注、真机、回传、评测 | 90 天 | **不自动删** | 只有管理员 |
| **B 重建很贵** | 加工产出、格式转换、数据工厂 | 30 天 | 不自动删 | 流程负责人 |
| **C 能重新拿** | 开源数据集、互联网抓取、编译产物 | 7 天 | 不自动删 | 管理员 |
| **C′ 过程记录** | 训练日志 | 3 天 | 90 天删 | 本人 |
| **D 发布物** | release、对外交付 | 永久 | **不可变、不删** | 只有管理员 |
| **过程产物** | 开发区 `ckpt/` | 3 天 | 非当前 3 天 / 当前 30 天 | 本人 |
| **暂存** | `_staging/` | 不开 | 7 天删 | 自动 |
| **杂项** | `_misc/<人>/` | 30 天 | **不自动删**，体检报 | 本人 |
| **副本** | 非杭州地域的同一份数据 | 3 天 | 30 天删 | 自动 |

**A 档里标注最贵**：其余是重新采集（花时间），标注是重新雇人（花钱且慢）。
回传数据同理，重跑一次要重新占用机器人和场地。

**这张表和现网差距很大，要认**：今天 13 个自管桶里 12 个是**整桶一条 3 天旧版本清理**，
不分前缀不分档。要落这张表，得把整桶那条拆成按前缀的多条规则。
拆之前先做一件事 —— 给 `wuji-bucket-hangzhou` 补上缺失的那条（第一节），
它现在是**完全没有**，不是配得不够细。

---

## 五、九个一级目录：一套词表，所有数据桶通用

上一版给每个桶单独设计目录，结果是「新接一个桶，先想它该长什么样」。
而 **OSS 没有真目录**这件事让统一几乎零成本：没用到的一级目录就是不存在。
所以不搞子集 —— **所有数据桶一律同一套 9 个**，只有收货桶是例外。

| 一级目录 | 放什么 | 档 |
|---|---|---|
| `third-party-data/<来源>/<批次ID>/` | 外来原始：供应商交付、互联网抓取 | A |
| `raw/<来源>/<批次ID>/` | 自产原始：teleop / factory / dc | A |
| `public-datasets/<数据集名>/` | 开源，能重下 | C |
| `label/<来源>/<版本>/<批次ID>/` | 标注成品 | A |
| `processed/<来源或项目>/<批次ID>/` | 加工、格式转换、重定向 | B |
| `result/<项目>/<批次ID>/` | 实验产出、模型权重 | B |
| `release/<项目>/<版本>/` | 发布物、对外交付 · 不可变 | D |
| `general/<登录名>/` | 通用 / 待认领 | — |
| `_staging/<批次ID｜链ID>/` | 临时，自动清 | — |

批次根只放一个 `_manifest.json`；再往下是数据格式自己的事。

**为什么 `release/` 必须是一级目录、不能是 `result/<项目>/release-*/`**：
`oss:Prefix` 只能前缀匹配，中段通配写不出来。**任何需要独立保留策略或独立权限的东西，
只能是一级目录** —— 这是云侧的硬约束，不是设计偏好。同一条约束也解释了第八节③
（`third-party-data/*_trans/` 那个权限写不出来的问题）。

**互联网数据和供应商交付同进 `third-party-data/`**，靠 `<来源>` 那一层分开。
两者档位相同（都是外来的、都要过 QC、都可能有 license 约束），拆成两个一级目录
只会让「这算供应商还是抓的」变成一个要问人的问题。

### 统一之后，三套规则各自缩成一条

| | 规则 | 为什么能缩成一条 |
|---|---|---|
| **权限** | `general/<登录名>/` 是唯一给人开写权限的前缀，其余只有流程身份能写 | 所有桶目录名相同 |
| **清理** | 只配在 `_staging/`（7 天）。其余一律不自动删 | 不用再记「哪个桶是中转」 |
| **同步** | 搬运永远是「只换桶名」，key 变了就是 bug | 两地路径必然相同 |

各桶的差别从「结构不同」变成「**哪些目录该有东西**」—— 而这是**体检项，不是规范项**：

- 中转桶 `wuji-data-tran` 里出现 `_staging/` 和 `general/` 以外的数据 → 报出来。
  今天实测那 1070 个 lightwheel 目录和一整套 ckpt 正好会被这一条抓到。
- 曼谷 `wuji-bangkok` 里出现杭州没有的前缀 → 报「孤本在副本桶」。今天实测 5/5 全中。
- 任何桶的桶根直接放对象（不在九个一级目录下）→ 报出来。

规则不必跟着桶的用途变；用途变了体检自己会说。

### 收货桶：唯一的例外，也是唯一要新建的桶

```
★wuji-inbox-hz/                 杭州 · 全公司唯一对外可写
 ├── third-party-data/<来源>/<批次ID>/   路径和主本一字不差
 └── _quarantine/<来源>/<批次ID>/        QC 没过的，不删

         │ QC passed → **只换桶名，前缀一个字不改**
         ▼
   wuji-bucket-hangzhou/third-party-data/<来源>/<批次ID>/
```

**这里刻意不给那 9 个。** 它是权限边界，多一个前缀就多一块外部能写的地方。
别的桶多一个空目录没代价，这个桶有。

供应商凭证只签到 `third-party-data/<自己>/` 这一格 —— 看不见别的供应商，更看不见主本。
交付完 QC 跑在收货桶里，`passed` 才搬进主本。**验收之前，主本一个字节都没有。**

**搬运是「同 key 换桶」。** 这不只是省事：key 一旦在搬运中被改写，血缘就断了
（血缘靠的是同一个批次 ID 贯穿各级目录），而且面板的迁移引擎再也没法用
「源 key 集合是否全部出现在目的」这一条来校验搬完整没有。
**任何会改变 key 的路径都是 bug。** 火山 DMS「目的只到桶级」这个限制，在这条链上正好是特性。

搬完源批次保留 30 天再清（留校验窗口）；`_quarantine/` **不配任何清理** ——
QC 没过的是供应商索赔的证据。

### 目标 tree

`★` = 新增要建，`〔存量〕` = 原地不动。

```
═══ 所有数据桶 · 同一套 9 个，无子集 ═══════════════════════════════

阿里 杭州   wuji-bucket-hangzhou        859.7 TiB   主本①，有交付入口
阿里 新加坡 wuji-sing                    75.6 TiB   主本②，无交付入口
阿里 杭州   wuji-egocentric-processed    32.4 TiB   ego 加工链
阿里 杭州   wuji-data-tran               23.9 TiB   搬运落脚点
阿里 曼谷   wuji-bangkok                 13.6 TiB   就近副本
阿里 北京   wuji-test-data               39.7 GiB   训练产物 / 编译
阿里 深圳   data-factorys                2.99 GiB   数据工厂
火山 上海   wuji-dc-shanghai · data-tran · wuji-ego-processed · …

 ├── third-party-data/<来源>/<批次ID>/
 ├── raw/<来源>/<批次ID>/
 ├── public-datasets/<数据集名>/
 ├── label/<来源>/<版本>/<批次ID>/
 ├── processed/<来源或项目>/<批次ID>/
 ├── result/<项目>/<批次ID>/
 ├── release/<项目>/<版本>/
 ├── general/<登录名>/
 └── _staging/<批次ID｜链ID>/

     用到才出现 —— 不预先建占位目录

═══ 收货 · 唯一的例外 ═══════════════════════════════════════════════

★wuji-inbox-hz/    third-party-data/<来源>/<批次ID>/   ＋  _quarantine/<来源>/<批次ID>/

═══ 各桶的存量（原地不动）与要迁进 general/ 的人名目录 ════════════════

wuji-bucket-hangzhou    〔存量 35 个一级目录不动〕opensource_dataset/
                         third-party-data-labelv1/ worldmodel/ mask/ egoscale/
                         ego_pretrain/ w0/ wuji-il/ wuji_openpi/ research/ model/ …
                         zijie_youtube/ volcano/ 留在 third-party-data/ 下不动
                         旧路径各放一份 _DEPRECATED.md 指向权威路径
                        ★6 个人名目录迁入 general/：
                         caomaosong chenxinyi guanqihe pengjw zexianji zhangyl

wuji-sing               〔孤本 · 只此一份，永不清〕worldengine/ vitra/ PalmDex/
                         RekaDaily-10k-raw/ models/ pretrain_processed_data/
                         data-line-test/ openwam-miles-test/
                        〔副本 · 杭州也有，可配清理〕egoscale/ worldmodel/
                         third-party-data/ w0/
                        ★4 个人名目录迁入：guanqihe wangyizhou wangyuran xiaoxiong
                         **新代码一律不进 OSS** —— egoscale/code、w0/code 是存量，不动

wuji-egocentric-processed
                        〔存量〕extracted/（新增改进 processed/）
                                results/（带 s，放 _DEPRECATED.md，新增一律写 result/）

wuji-data-tran          〔存量 · 不是中转物，要单独认领归位〕
                         lightwheel/（1070 个任务目录 → 该回主本 third-party-data/）
                         vitra_sft_…_20260608_0616/（整套 ckpt → 该回本人开发区）
                         vitra/ data_conform/ dataset_test/ ossutil_output/
                        ★9 个人名目录迁入：chenrankou chuzhong guanqihe qianbsh
                         wangyuran xiaoxiong yuboguo zexianji zhangchenhao
                        ★清理只配 _staging/，**绝不配整桶**
                         现在的 pfs-staging/ 并进 _staging/，少一个专用前缀

wuji-bangkok            〔存量〕lightwheel/ vitra/
                         **实测 5/5 全是孤本 —— 不配任何清理**
                        ★3 个人名目录迁入：chenrankou wangyuran zhangchenhao

wuji-test-data          〔存量〕gmt-ckpt/ model/ models/ 不动。新 ckpt 一律落开发区

data-factorys           〔存量〕test/20260723/ 不动

wuji-dc-shanghai (TOS)  现状的 <YYYYMMDD>/ 就是 raw/dc/<批次ID>/ 的形状
                        **不加 inbox** —— 自采不经外部，没有权限边界要守
                        ★3 个人名目录迁入：jichuan wangchunlin xiaoxiong

data-sync-b2d (TOS)     ★8 个人名目录迁入：handuo huangsiqiao lzicong pengjw
                         qianbsh zhangwt zhujl zhuzijie

wuji-datasets-hz-6c661af0 · rl-data · wuji-rl-dataset · ai-prod-wj-wl-oss
umi-tos-* · data-infra-sha · ego-output · eog-pretrain-code · wuji-egocentric-data
                        只登记进 identity/bucket-notes.json，结构待各自负责人确认
                        ★TOS 的 wuji-ego-processed 有数据 ——
                         别跟着把阿里那个已删的同名空桶一起删

═══ 开发区 · 形状本来就不同（以人为主，不是以数据为主）═══════════════

wuji-algo-dev-hz/  ·  wuji-algo-dev-sing/
 └── <组>/<人>/ckpt/<实验名>/    非当前 3 天 / 当前 30 天
     组：algo  data-collect  data-sys  imitation  inference
         posttrain  pretrain  proprio  rl  ★platform（原 general，改名避让）

═══ 别碰 ═══════════════════════════════════════════════════════════

cri-*-registry ×10   h2r-dlc-*   oss-pai-*   data-infra-emr-log-hgh
las-datastore   ml-platform-auto-created-* ×3            （见第二节）
```

开发区原有的 `general/` 组要**改名成 `platform/`**：否则 `general` 在两个语境下各指一件事
（数据桶里是「待认领」，开发区里是一个组名）。两个开发桶今天都是 0 字节，
这是整份规范里最便宜的一处改动。

### 每个桶干什么用

**除收货桶外一个新桶都不建。** 左边是角色，右边是它落在**已经存在**的哪个桶。

| 角色 | 档 | 桶 | 地域 | 今天的状态 |
|---|---|---|---|---|
| 外来数据收货 | A | ★`wuji-inbox-hz` | 杭州 | **要建** |
| 外采 + 开源 + 互联网 + 自采混放 | A/B/C | `wuji-bucket-hangzhou` | 杭州 | 859.7 TiB，45 个顶层目录 |
| ego 加工流水线 | A/B | `wuji-egocentric-processed` | 杭州 | 32.4 TiB |
| 数据工厂 | B | `data-factorys` | 深圳 | 独立桶，几乎还没开始用 |
| PAI 数据集挂载 | A/C | `wuji-datasets-hz-6c661af0` | 杭州 | 13.4 TiB，`.dlsdata/` 是 PAI 的签名 |
| 训练产物 / 编译缓存 | C/C′ | `wuji-test-data` | 北京 | 名字说谎，内容对 |
| 个人开发区（含 ckpt） | — | `wuji-algo-dev-hz` / `-sing` | 杭州/新加坡 | 目录建好了，**还是空的** |
| 跨地域搬运 | — | `wuji-data-tran` → `wuji-sing` → `wuji-bangkok` | 杭州/新/曼谷 | 这条链在跑 |
| RL / forge / lakefs | — | `rl-data` / `wuji-rl-dataset` | 呼/杭 | 自带版本机制，只登记不改 |
| 产线 | — | `ai-prod-wj-wl-oss` | 乌兰察布 | 1.93 TiB，**归属待确认** |
| 火山侧数采 | A | `wuji-dc-shanghai` | 上海 | 日期目录，每天在写 |
| 火山侧 ego + 开源 | A/C | `wuji-egocentric-data` | 上海 | — |
| 火山侧加工镜像 | B | `wuji-ego-processed` | 上海 | 阿里同名桶已删，**这个别删** |
| 火山侧同步中转 | — | `data-sync-b2d` / `data-tran` | 上海 | — |
| UMI 流水线 | A/B | `umi-tos-raw`→`-lerobot`→`-lance` | 广州 | 三级，只登记不改 |

### 两处要跟着改的配置

1. **`pfs-staging/` 并进 `_staging/`** —— 要改 bot 的 `PFS_STAGING_MAP` 一行（指向 `_staging`）。
2. **开发区 `general/` 组改名 `platform/`** —— 0 字节，改名最便宜。

---

## 六、路径与命名

```
<桶>/<一级>/<二级>/<批次ID>/<内容>
<桶>/<一级>/<二级>/<批次ID>/_manifest.json      ← 必须有
```

### 批次 ID

```
<采集日期>-<来源>-<场景>[-<序号>]

20260920-ego-kitchen
20260920-robot-pickplace-02
20260918-public-openx
```

- **日期在前**：OSS 按字典序返回，列举就是时间线。`wuji-dc-shanghai` 的 97 个日期目录
  已经是这个形状，它是对的。
- **来源第二**：`ego` / `robot` / `public` / `web` / `factory`。
- 字符集只用 `[A-Za-z0-9][A-Za-z0-9._-]{0,62}`。这一段会进 OSS key 和 RAM 策略的
  `oss:Prefix` 条件，一个 `*` 或 `../` 就能让一条策略覆盖到别人的数据。

**同一批数据在各级目录下用同一个 ID。** `raw/20260920-ego-kitchen/` 加工之后是
`processed/20260920-ego-kitchen/`，标注之后是 `label/20260920-ego-kitchen/`。
血缘就是 ID 相同，列一层就知道这批走到哪一步了。

### `_manifest.json`

```json
{
  "batch": "20260920-ego-kitchen",
  "stage": "raw",
  "source": {"kind": "ego", "device": "...", "collected_at": "2026-09-20"},
  "upstream": [],
  "produced_by": {"model": "", "ckpt": "", "code": ""},
  "qc": {"status": "pending|passed|failed", "at": "", "by": "", "report": ""},
  "license": "",
  "owner": "zhangsan",
  "replicas": [],
  "note": ""
}
```

`upstream` 记上游批次 ID（可多个），加工/发布必须填，原始为空。
`produced_by` 只有回传数据和 ckpt 填 —— 前者记「哪个模型跑出来的」，后者记「哪份代码训的」。
**数据飞轮的血缘是有环的**（模型 → 回传数据 → 新模型），两个字段各记一边才闭得上。
没有 `produced_by`，半年后发现某批回传数据有系统性偏差，追不到是哪个模型版本产生的，
只能把那段时间的回传数据全部作废。

互联网数据的 `source` 必须有抓取时间和来源站点；开源数据的 `license` 必须填。
这两项不是登记癖好，是出合规问题时唯一能自证的东西。

---

## 七、目录名的硬规则

今天实测到四个坏名字，都不是洁癖问题，都有具体后果：

| 实测到的 | 在哪 | 毛病 |
|---|---|---|
| `third-party-data/ shutu/` | 阿里 杭州 | 前导空格 |
| `wuji-dc-shanghai/lerobot v3/` | 火山 上海 | 中间空格 |
| `wuji-egocentric-data/ egocentric--processed/` | 火山 上海 | 前导空格 + 双横线 |
| `eog-pretrain-code/` 下名为 `/` 的目录 | 火山 上海 | 空目录名 |

**规则：目录名只用 `[A-Za-z0-9][A-Za-z0-9._-]*`。不准有空格，不准空名，不准前后留白。**

后果具体：SSH 迁移链、PFS 直传、面板的路径校验**三处都会直接拒绝带空格的路径**。
SSH 那条是因为路径要穿过 bot → 新加坡 shell → 泰国 shell 三层解析，空格在那里会被拆成
两个参数。**也就是说带空格的目录没法用现有链路同步到新加坡和泰国**，而这些恰恰是最该往
算力所在地同步的数据。空目录名更糟：列举返回的 key 是 `/`，大多数路径拼接代码拼出来是 `//`，
能不能访问到看 SDK 心情。

另外三个是拼写/嵌套错误，**只登记不改名**（改名会打断引用）：

- `wuji-egocentric-data/opic-titchen/` —— `epic-kitchen` 打错了，和同桶的 `epic-kitchen/` 并存
- `wuji-bucket-hangzhou/wuji-data-driven-caliber/wuji-data-driven-caliber/` —— 嵌套重了一层
- `third-party-data/bc_z/bc_z/` —— 同样重了一层，疑似导入脚本 bug

---

## 八、三个必须说清楚的现状冲突

### ① 开源数据集现在有三个落点

```
wuji-bucket-hangzhou/public-datasets/     ← 规范定的，**今天是空的**
wuji-bucket-hangzhou/opensource_dataset/  SynData/ egodex/ egoverse/ larybench/
wuji-bucket-hangzhou/third-party-data/    adt/ aether/ droid/ kuka/ … 约 24 个
```

`public-datasets/` 是上一轮定下来的，目录建了，一个数据集都没进去；与此同时
`opensource_dataset/` 里已经有 4 个。**两个同义目录并存，意味着下次有人加开源数据集会凭印象
二选一，最后两边都有一半。**

处置（不挪存量）：

- **新增一律进 `public-datasets/`。**
- `opensource_dataset/` 与 `third-party-data/` 下的存量开源数据集**原地不动**，
  在 `opensource_dataset/` 下放一个 `_DEPRECATED.md` 指向 `public-datasets/`。
- 为什么非要和供应商目录分开：**档位不同**。供应商交付是 A 档（付费、拿不回来、QC 不过
  不该结算），开源是 C 档（能重下）。混在一个前缀里配不出不同的清理规则。而且 24 个开源目录
  把 9 个供应商淹没了，光看列表认不出哪些是花钱买的。

### ② 标注数据有三个落点

```
wuji-bucket-hangzhou/label/                          Haiyu_trans/ shutu/ worldengine/
                                                     real-world-ego/ vitra-1m/ nuoyiteng_lerobotv3_21/
wuji-bucket-hangzhou/third-party-data/label/         Haiyu_trans/ lightwheel/ shutu/ worldengine/
wuji-bucket-hangzhou/third-party-data-labelv1/label/ shutu/ worldengine/
```

三处都有 `shutu/` 和 `worldengine/`。**没人能确定哪个是权威的** —— 那就一定有人读错那个。

处置：**新增标注只写顶层 `label/`**（内容最全、层级最浅）。另两处不动，各放一份
`_DEPRECATED.md` 写明「权威路径是 `label/`，这里是历史数据」。不写这个文件，三个目录谁也
不知道该不该信，等于三份都不能用。

### ③ 格式转换输出和原始数据并排放，权限写不出来

`third-party-data/Haiyu/` 是 A 档只读，`third-party-data/Haiyu_trans/` 是 B 档加工流程可写，
**它俩在同一层**。OSS 的 `oss:Prefix` 条件只能前缀匹配、不支持中段通配 —— 写不出
「可写 `third-party-data/*_trans/`」。只剩两个选择：给整个 `third-party-data/` 写权限
（那原始数据也能删了），或者新的转换输出换个位置。

处置：**新增转换输出写 `third-party-trans/<供应商>/<格式>/`**（今天这个目录还不存在，要建）。
和已有的 `third-party-action/`、`third-party-data-labelv1/` 是同一套命名。
存量 `Haiyu_trans` / `nuoyiteng_lerobotv3` / `nuoyiteng_lerobotv3_21` **原地不动**。

火山那边同样的问题更严重：`wuji-dc-shanghai` 下有 `trans/` `trans_bak/` `trans_backup/`
`trans_for_encode/` `Trans_deploy/` `tianji2_trans_wrist_cam/` **六套转换目录**。
本规范不动它们（存量），但新的转换产出别再加第七套。

---

## 九、每个桶都有 `_misc/<人>/`

沿用旧版。没有归宿的东西不会消失，它会落在**桶根** —— 而桶根是最糟的去处：没有生命周期、
没有主、`oss:Prefix` 条件也没法单独管它。今天实测到的桶根散落文件：
`wuji-rl-dataset/uploadFile.txt`、`data-sync-b2d/bandwidth_test`（512 MiB 测速文件）、
`data-tran/` 下两个 dms 自测报告、`wuji-egocentric-data/wuji-vitra.tar.gz`（4.7 GiB）。

```
<任意桶>/_misc/<登录名>/
```

1. **桶根不准放对象。** 不属于任何一级目录的，进 `_misc/<你的登录名>/`。
2. **必须有主。** 少了 `<人>` 这一层就不合法 —— 桶根垃圾的本质问题不是乱，
   是**没人能说它还要不要**。有主就永远有人可以问。
3. **这是数据桶里唯一给人开写权限的前缀。** 你永远有地方放东西，
   所以不需要为了「就放一个文件」去申请数据前缀的写权限。
4. **不自动删，但体检会报。** 超过 90 天或超过 10 GiB 的条目列出来问主人。
   压力来自可见性，不来自定时器。
5. **`_misc/` 不是 `_staging/`。** staging 是**明知临时**、7 天自清；
   misc 是**还不知道归哪类**、有主、不自动删。

> **为什么 misc 不自动删**：未分类 ≠ 没价值。自动删未分类数据只会教会人
> 「别放 `_misc/`，放桶根」—— 那就回到原点了。

### 人名目录的处置

今天实测，人名目录出现在**六个不该出现的桶**里：

```
wuji-bucket-hangzhou    caomaosong chenxinyi guanqihe pengjw zexianji zhangyl
wuji-data-tran          chenrankou chuzhong guanqihe qianbsh wangyuran
                        xiaoxiong yuboguo zexianji zhangchenhao
wuji-sing               guanqihe wangyizhou wangyuran xiaoxiong
wuji-bangkok            chenrankou wangyuran zhangchenhao
data-sync-b2d(TOS)      handuo huangsiqiao lzicong pengjw qianbsh zhangwt zhujl zhuzijie
wuji-dc-shanghai(TOS)   jichuan wangchunlin xiaoxiong
```

**中转桶里的人名目录是最危险的一类**：`wuji-data-tran` / `wuji-sing` / `wuji-bangkok`
按中转桶的定位是可以激进清理的，而人把长期数据放在那儿 —— **哪天按中转配清理规则，
这些就没了，而放的人完全不知道**。

处置：**不自动迁移，靠本人认领后自助搬**。自动归属本来就不可靠 —— 目录名和 RAM 登录名
对不上（`yuboguo`→`guoyubo`、`qianbsh`→`qianbinsheng`、`zhangwt`→`zhangwentao`、
`zhukefei`→`liyi`），为十来个人建带状态机的迁移流水线不划算。
但**在清理规则上线前必须先问一遍**，问完没认领的才按中转清。

---

## 十、地域：杭州是入口，不是「每个地域一套」

```
杭州        入口 + 主本
其他地域    副本，为了算力就近
```

实测对应关系：

- **主本在杭州**：`wuji-bucket-hangzhou`（859.7 TiB）/ `wuji-egocentric-processed`（32.4 TiB）/
  `wuji-datasets-hz-6c661af0`（13.4 TiB）
- **副本走现有中转链**：`wuji-data-tran`（杭州出口，23.9 TiB）→ `wuji-sing`（新加坡，75.6 TiB）
  → `wuji-bangkok`（曼谷，13.6 TiB）。bot 的 SSH 迁移链、桶间迁移、PFS 直传都跑在这条链上，
  **不要再造一条**
- **开发区是例外**：`wuji-algo-dev-hz` / `-sing` 各地域独立，人在哪干活就在哪写，不互为副本
- **四个桶不在杭州**：`data-factorys` 深圳、`wuji-test-data` 北京、`ai-prod-wj-wl-oss` 乌兰察布、
  `rl-data` 呼和浩特。**不算违规，但跨地域读它们会产生公网流量费**，别在杭州的训练任务里直接挂

副本三条规则：

1. **不预先同步。** 只有真的有任务要在那个地域读，才建副本 —— 「先同步过去备着」是最容易
   产生的浪费：21 TB 传一次是小时到天级，而大部分批次从头到尾只在一个地域被读过。
2. **副本是显式操作，记进 manifest**：
   `"replicas": [{"region":"sing","at":"2026-09-20","by":"<job_id>"}]`。
3. **副本配生命周期，主本绝不自动清。** 副本是为当下训练就近读，任务跑完就是纯成本；
   建议 30 天清理，要用再同步。

**一条要标出来的**：旧版写「走专线过去」。实测现有的杭州→新加坡→曼谷链走的是公网 ossutil
（289 MB/s），**不是专线**。目前这句在本规范里是**目标**，不是现状。

---

## 十一、QC

QC 不是一个目录，是**批次的状态机**。数据写完那一刻它就存在，只是还没有结论。

```
写入完成 → _manifest.json  qc.status = "pending"
                ↓  QC 流程读数据、跑检查、写回
        passed ──→ 下游（标注 / 加工 / 训练）才允许读
        failed ──→ 停在原地，不删
```

1. **结论写回源批次的 manifest，不另起炉灶。** 「这批能不能用」必须只有一个权威答案。
   QC 结论另放一处，就会有人直接读原始目录跳过它。
2. **报告和数据同批次同前缀**：`<桶>/<一级>/<批次ID>/_qc/report.json`。
   报告里必须列出坏样本的**完整 object key** —— 只给一个「通过率 92%」的数字，
   下游没法知道该跳过哪些文件。
3. **`failed` 的批次不删。** 它是负样本，更是**供应商索赔的证据**。外采数据 QC 不过要能
   拿着 `_qc/report.json` 去对账 —— 删了就只剩口头说法。
4. **`qc.status` 只有 QC 流程身份能写。** 人能改等于能绕过检查，那这套就只是装饰。
5. **`pending` 超时要报出来。** 写完没人跑 QC 的批次会一直 pending，而 pending 对下游
   等于不可用 —— 数据躺在那儿花着钱没人用，也没人知道。

**外采数据的 QC 门最硬**：供应商交付是 A 档且**要付钱**，`passed` 之前不进入任何下游流程，
也不该结算。

现网已经有一套在跑：`wuji-bucket-hangzhou/mask/qc_standard-e5d96504/` +
`wuji-dc-shanghai/qc_previews/`（火山）。**没查它和上面这套是什么关系**，要人确认。

---

## 十二、权限

| 一级目录 | 算法组（自己的 AK） | 标注人员 | 加工流程 | 数据工厂 |
|---|---|---|---|---|
| `third-party-data/` `teleop/` `raw/` | 只读 | 只读 | 只读 | — |
| `label/` | 只读 | **读写** | 只读 | — |
| `processed/` `extracted/` `result/` `third-party-trans/` `third-party-action/` | 只读 | — | **读写** | — |
| `public-datasets/` | 只读 | — | — | — |
| `release/` `delivery/` | 只读 | — | — | — |
| `data-factorys` 整桶 | 只读 | — | — | **读写** |
| `wuji-algo-dev-*/<组>/<自己>/` | **读写 + 删** | 同左 | 同左 | 同左 |
| `_misc/<自己>/` | **读写 + 删** | 同左 | 同左 | 同左 |

**写入一律走流程身份，不给人。** 人要写只有两条路：申请临时凭证（有时限、有范围、有台账），
或者在自己的开发区里随便写。

### 一条必须说清楚的限制

**上面这张表管不到 DSW/DLC 里跑的代码。** PAI 用服务角色访问 OSS，
`AliyunPAIDSWDefaultRolePolicy` 的 `oss:GetObject/PutObject/DeleteObject` 是
`Resource: *` 且**无 Condition** —— 人在 notebook 里能读写删任意桶，和他自己挂什么策略无关。

所以这张表挡的是「从笔记本跑脚本手滑删到别人桶」，**不是隔离**。要真隔离得给数据集指定
`DatasetTaskRamRole` 或收窄服务角色，那会影响所有训练任务，是另一件事。

### 对外交付

外采是别人的数据进来，交付是**我们的数据出去 —— 出去了收不回**。三条硬规则：

1. **绝不从源桶直接交付。** 每次交付复制成快照
   `wuji-egocentric-processed/delivery/<交付ID>/`。直接给源桶凭证意味着对方看得见目录结构，
   而且前缀写宽一点就多给了。
2. **交付记录比数据活得久。** 数据可以到期删，但「什么时候、给了谁、哪些文件、依据哪份协议」
   必须永久留 —— 出合规纠纷时这是唯一能自证的东西。
3. **交付 ID 必须含接收方**：`20260920-nuoyiteng-egoset-v1`。

bot 的 `temp_ak_issuance`（审批通过发时限 OSS 凭证）已经是这条路的后半段，
缺的是前半段：交付什么、从哪复制、记在哪。

---

## 十三、checkpoint 落在个人开发区

`wuji-algo-dev-hz|sing/<组>/<人>/ckpt/<实验名>/`。开发桶各地域独立，人在哪训就在哪写。
训练时 ckpt 堆在自己空间，**好的那个才发到 `wuji-egocentric-processed/release/`**
（D 档，不可变、长保留）—— 线上在跑的模型删不得，而「能重算」在故障恢复的时候不是安慰。

**今天两个开发桶都是 0 字节**，说明这条还没真正执行，ckpt 还堆在五个地方：
`wuji-test-data/gmt-ckpt/`、`wuji-bucket-hangzhou/checkpoints/`、
`wuji-bucket-hangzhou/chenxinyi/ckpts/`、`wuji-bucket-hangzhou/pengjw/ckpt/`、
`wuji-bucket-hangzhou/cpfs-to-vepfs/ckpt/`。存量不搬，新的往开发区落。

三个要一起解决的问题，否则开发桶会变成最大的成本黑洞：

**① 开发桶的版本控制对 ckpt 是负担。** 两个开发桶都开了版本控制 + 3 天旧版本清理。
而训练通常反复覆盖 `latest.pt` —— 每覆盖一次留一个旧版本，3 天内的每一次都在计费。
**ckpt 一律写步数编号的文件**（`step_040000.pt`），「最新」用一个几十字节的 `latest.json`
指过去，不要覆盖大文件。覆盖小指针文件，版本控制的开销可以忽略；覆盖 10 GB 权重则不然。

**② 中间 ckpt 要有自动清理，否则开发区只涨不落。** `ckpt/` 前缀单独一条生命周期：
**非当前版本 3 天、当前版本 30 天**。人要长期留的自己搬到 `release/`
（那是个显式动作，也正好是「我认为这个值得保存」的表达）。

**③ 大文件写 OSS 挂载点有个实测过的坑。** ossfs2（FUSE）**只支持顺序写**，
而超过 100 MiB 的对象默认会被切成分片并发 pwrite 到不同 offset —— 实测 19.5 TiB 那次：
≤100 MiB 的 33484 个全成功、>100 MiB 的 42366 个**全部失败**（`invalid argument`），
边界与分片阈值严格吻合。ckpt 动辄几 GB，正好在这个雷区里。
**所以 ckpt 不要写挂载点，用 SDK / ossutil 直接传。**
PAI 的挂载实现是哪一种、对大文件怎么分片写，**没验证过**。

---

## 十四、存量怎么办

**不搬。** 存量纳管，新增按标准。

- `identity/bucket-notes.json` 登记每个存量桶是什么、能不能删、谁负责。
  今天只填了 7 个，还差 6 个阿里桶 + 全部 8 个火山桶
- 体检的「没登记的桶」那一类读它，报出来时带上用途，不再只有一个骗人的名字
- 个人目录散在非开发桶里的（实测 6 个桶、约 30 个目录）靠本人认领后自助搬，不建自动迁移

---

## 十五、还没查清的

按「不确认会出事」的程度排：

1. **`wuji-bucket-hangzhou` 缺旧版本清理规则** —— 859.7 TiB 的桶开着版本控制没有清理，
   旧版本无声堆积。**这一条最该先动**
2. **中转桶里的人名目录**（`wuji-data-tran` 9 个 / `wuji-sing` 4 个 / `wuji-bangkok` 3 个）——
   按中转配清理会删掉别人以为是长期存放的东西。上线清理规则前必须先问一遍
3. **火山 `wuji-ego-processed` 和阿里已删的同名桶** —— 名字只差一个词，
   登记里必须写明「火山这个有数据，别跟着删」
4. **`ai-prod-wj-wl-oss`（产线，1.93 TiB）归谁管** —— 本规范管不管它。
   它只有一个 `ali_ppu_test/` 目录，名字像测试，容量不像
5. **`wuji-datasets-hz-6c661af0`（13.4 TiB）的归属** —— `.dlsdata/` 说明是 PAI 建数据集时
   自动开的，但里面 121 个 worldengine shard 是真数据。它是 PAI 托管还是人在直接写
6. **`third-party-data/` 下 5 个无主目录**：`v1.0` `vl` `w0` `w0-multimodal` `rock-climb-pilot`。
   `vl/` 里有一整套 `_audit/ _control/ _manifests/ _status/ _tools/` 自带流程的东西，
   `w0/` 只有 `code/`，看着像模型代号不像数据集。认不出来的一律标「无主」，那本身就是待办
7. **`rl-data` / `wuji-rl-dataset` 的 forge / lakefs 流水线归谁管** —— lakeFS 是数据版本控制
   系统，可能自带版本和发布机制。如果是，它不该被套进本规范，两套版本管理叠在一起只会
   互相打架。确认前这两个桶按「已有自己的流程」对待
8. **`umi-tos-*` 五个桶归谁管** —— `umi-tos-internet` 和 `umi-tos-internet-lance` 今天是空的，
   是新建待用还是已废弃
9. **QC 现网那套和第十一节的关系** —— `mask/qc_standard-e5d96504/` 与 `qc_previews/` 没查
10. **各档的版本保留天数**（90/30/7）是拍的，要按实际容量和成本调。今天现网是一刀切 3 天
11. **个人开发区要不要配额** —— ckpt 落开发区后它会成为最大的存储消费方，
    现在没有配额也没有「谁占了多少」的可见性。建议先做可见性
12. **标注人员如果引入外包**，`label/` 的权限模型要重做（外部人员不该有 RAM 账号，
    得走临时凭证）

---

## 十六、怎么开始

按这个顺序，每步都能单独完成、单独回退：

| # | 做什么 | 动了什么 | 可逆 |
|---|---|---|---|
| 1 | **给 `wuji-bucket-hangzhou` 补一条 `NoncurrentVersionExpiration`** | 一条 OSS 规则 | 是 |
| 2 | `identity/bucket-notes.json` 把 13 个阿里桶 + 8 个火山桶填完（现有 7 个） | 只是登记文件 | 是 |
| 3 | 建 `third-party-trans/`，在 `opensource_dataset/` 和两个旧 label 路径放 `_DEPRECATED.md` | 几个小对象 | 是 |
| 4 | 问一遍中转桶里那些人名目录还要不要 | — | — |
| 5 | 按第四节把整桶那条 3 天规则拆成按前缀的多条 | OSS 配置 | 是 |
| 6 | 给流程身份建 RAM 策略（第十二节的表） | 新增策略，不动现有 | 是 |
| 7 | 新数据按规范落，**存量一个都不搬** | — | — |
| 8 | 体检加三类：桶没登记、批次 QC 超时 pending、目录名带空格 | 面板 | 是 |

**第 1 步最该先做**：它是唯一一条「现在每天都在花冤枉钱」的，而且改一条规则就完事。
**第 2 步最便宜**：今天两个桶差点因为名字被当成可清理的，规范其余部分都是在管新数据，
只有登记能立刻止损存量。

# 当前工作流入口

`.github/workflows/` 只保留以下 18 个入口。文件名是自动续跑协议的一部分，不随页面名称或实现版本改名。

| 职责 | 页面名称 | 文件 | 行为 |
| --- | --- | --- | --- |
| 13 个厂商，各一个 | 固件采集 · 厂商 | `catalogue_<vendor>.yml` | 本地元数据容器增量采集、断点、加密快照；不写 D1 |
| 唯一目录入库入口 | D1 · 统一发布 | `catalogue_publish.yml` | 官包每 10 分钟批量同步；TG 发布最新审核合格批次 |
| TG 抓取 | TG · 抓取 | `telegram_fetch.yml` | 自动读取已有设备目录，执行一批检索，保存私有原始证据；不下载固件、不写 D1 |
| TG 清洗 | TG · 清洗 | `telegram_clean.yml` | 使用已有证据全量审核、补齐分类结构、隔离不合格项；不请求 TG、不写 D1 |
| 代码检查 | CI · 离线验证 | `ci.yml` | push/PR 离线测试，不执行迁移、修复、部署或生产写入 |
| 现有独立服务 | 服务 · 维护与检查 | `services.yml` | Sony 按需 ZIP 服务、公共索引同步、手动提取服务检查 |

厂商：ASUS、Google、Honor、Huawei、Motorola、Nothing、OnePlus、OPPO、Realme、Samsung、Sony、小米/Redmi/POCO。
Huawei/Honor 保留原禁用状态；Samsung 保留原自动运行开关；此次整理不启用任何停用来源。

## 数据流与暂停状态

官包：厂商采集 → 本地 SQLite / 加密快照 → D1 统一发布。

TG：抓取 → 私有状态仓库 → 清洗 → 私有审核批次 → D1 统一发布 → 现有网站页面验证。
清洗成功后通过 `workflow_run` 触发统一发布；也可手动清洗。自动链仅接受本仓库 main 的成功任务。

TG 抓取目前仍暂停：没有定时器或驻留交接，仅有手动入口，并要求仓库变量
`FMR_TG_FETCH_ENABLED=true` 才实际执行。此次整理不设置该变量。
不要求手输设备或频道。抓取内部的初筛仍保留，独立清洗负责最终生产审核。

TG 的抓取、清洗、发布共享原有并发锁，避免私有状态并发提交。
发布从审核报告读取原始 `audit_run_id`，不使用发布任务的新 run ID 寻找产物。
已发布且完成网站验证的批次不再请求 D1；网站验证失败时仅重试网站验证。
官包驻留发布和 TG 发布使用独立作业锁，因此等待官包下个时间窗口不会占用 TG 状态锁。

## 配置与兼容

- 继续使用现有 production Secrets、`FMR_CATALOGUE_STATE_DIR`、runner 路由变量及厂商续跑参数。
- 元数据与发布默认走本地容器；大流量 Sony ZIP 服务仍走云端。
- 保留厂商 `catalogue-v2-*` 并发锁和 `catalogue-v3-*` 加密产物名称。这些是兼容标识，不是多个生产版本。
- `FMR_D1_WRITES_PAUSED=true` 同时暂停官包与 TG 目录发布。
- Sony ZIP 每 15 分钟、公共索引每 6 小时的原定时规则保持；服务入口手动运行可选择具体任务。
- 部署文件的职责见 [`../deploy/README.md`](../deploy/README.md)。

## 历史入口

旧版、实验、单次修复与验收工作流移至 [`archive/workflows/`](archive/workflows/)，后缀 `.yml.disabled`。
保留完整原文供追溯，GitHub 不再将它们当作可执行工作流。旧运行历史仍可能出现在 Actions 历史页面；没有删除历史日志、检查点或生产数据。
历史脚本也保留，旧文档中的入口属于当时的部署记录；当前操作以本页为准。

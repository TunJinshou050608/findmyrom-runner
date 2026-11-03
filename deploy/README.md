# 部署入口

此目录管理 VPS 常驻提取服务，与 GitHub Actions 的采集、清洗和 D1 发布分开。
当前工作流清单见 [`../docs/workflows.md`](../docs/workflows.md)。

| 文件 | 职责 |
| --- | --- |
| `extractor/install.sh` | 首次安装 VPS 提取服务和专用 Tunnel |
| `extractor/fmr-extractor.service` | 提取服务 systemd unit |
| `extractor/fmr-extractor-tunnel.service` | 专用 Tunnel systemd unit |
| `extractor/fmr-extraction.slice` | 两项服务的共同资源限制 |
| `extractor/update-xiaomi.sh` | 历史小米支持补丁，固定校验当时的两个模块；不是通用更新器 |
| `extractor/README.md` | 安装、接口、检查和回滚说明 |

没有自动部署 VPS 的工作流。服务维护 Action 只做现有维护任务或手动检查，不会 SSH 修改 VPS。
历史补丁保留原路径以兼容已有部署记录；不要将其名称当作额外服务版本。

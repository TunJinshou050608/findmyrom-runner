# VPS 按需流式提取服务

网站验签、签发分区绑定短票据 → Worker 发送 HMAC 签名任务 → 独立 Cloudflare Tunnel → VPS 本机 18765 → 官方 CDN Range 读取 → 增量解密/解压 → HTTP 流式回传 → 浏览器下载原生 img。

## 与代理的隔离

- 专用账号 fmr-extractor、fmr-tunnel；专用 /opt/fmr-extractor、/etc/fmr-extractor 与两个同名前缀服务。
- 安装程序不读取或修改 Xray/Hysteria 配置、TLS 私钥、端口、路由、防火墙或服务；不需要 root SSH 密码进入部署包。
- 运行时使用只读文件系统，隐藏 /etc、/run、/var、其他 /opt 内容与用户目录，仅恢复程序、公共 CA、DNS/NSS 文件；/proc 隐藏其他账号进程。
- Python trust_env=False；清除代理环境变量。网络请求直连官方 CDN，不主动使用 VPS 的代理。
- VPS 仅主动连出隧道，本机 HTTP 监听 127.0.0.1，不开放公网 HTTP 端口。独立隧道不暴露 SSH 或其他服务。
- 同一 VPS 仍共享内核与物理资源；这是进程/cgroup/挂载隔离，不是独立虚拟机。无法保证已有系统级透明代理规则不会作用于出站连接；安装器不会检查或改动这些规则。

## 资源与失败处理

并发 1；忙时 429 和 Retry-After，不积压无界队列。默认输出限速 2,500,000 字节/秒（20 Mbps）；这限制镜像输出，不是网卡全部瞬时流量或上游下载速率。提取器与隧道共同 CPU 上限 60%、内存上限 448 MiB；提取器单独上限 352 MiB，隧道 96 MiB。无 Swap 依赖。任务上游读取预算 250 MiB、480 秒期限；不支持时拒绝，不降级为整包下载。解码过程中不创建官包或镜像文件。

单任务使用同步拉取/阻塞写入提供背压；断开后关闭生成器和 HTTP 客户端。已在进行的上游请求可能等到套接字超时（30 秒）才完全退出。CRC/SHA/解码检查失败时不发送最后保留块或 chunked 结束标记，浏览器不得获得一个伪装成成功的完整响应。

## 接口

- GET /images/{fw_token}/{boot|init_boot|recovery}：验签并展示下载确认页。
- POST /api/extract-ticket：同源表单，签发绑定固件与分区的 120 秒票据，303 转到下载接口。
- GET /api/extract?token=...&partition=...&exp=...&sig=...：验票据、验数据库原签名、解析原版本官包链接，转发流。
- VPS POST /v1/extract：最多 8 KiB JSON；schema/id/expires/token/brand/device/version/partition/package_type/url；三星另含 model/region。
- X-FMR-Signature = HMAC-SHA256(专用密钥, UTF-8(`token=extract-job:` + 原始 JSON + `&exp=` + expires))。原始 JSON 必须逐字节一致；90 秒有效期和进程内任务 ID 防重放。
- 浏览器不能提交 URL、解密密钥或命令；VPS 不持有网站票据密钥、GitHub Token、D1 凭据或代理凭据。
- 不支持 HEAD 启动、Range 恢复或断点续传；中断后重新下载。无 D1 轮询、任务写入或镜像缓存。

## 范围

复用已有 Google Factory、欧加/Vivo 全量 Payload 和三星 ZIP/AP/TAR/LZ4/AES 提取器；未知格式、源分区依赖、超预算、缺失目标分区均拒绝。官方 CDN 白名单沿用已测试域名，不承诺所有同品牌地址可用。三星加密路径只在 FUS 当前可用版本与所点版本精确一致时下载，历史版本不自动替换成最新版。三星提前停止读取 AP 时无法验证整个 AP 的 CRC/整包哈希。

当前实现不自动开放 system/vendor 等大分区，也不改变采集、已存镜像或原官包下载入口。

## 安装与回滚

将专用部署包上传 VPS，解压后执行 bash install.sh。需要 Linux x86_64、Python >=3.10（含 venv）、systemd >=247、启用 CPU/内存控制器的 cgroup v2、curl、sha256sum；缺依赖时安装器停止，不自动安装/升级系统软件。独立 cloudflared 固定 2026.10.7 并校验官方发布 SHA256。

安装器只启动 fmr-extractor.service 和 fmr-extractor-tunnel.service；已有服务保持原状。第二次安装不会自动重启正在运行的服务；更新后可单独 restart 这两个服务。

检查：systemctl is-active fmr-extractor.service fmr-extractor-tunnel.service

日志：journalctl -u fmr-extractor.service -u fmr-extractor-tunnel.service -n 40 --no-pager

停用：systemctl disable --now fmr-extractor.service fmr-extractor-tunnel.service

彻底卸载时，仅删除上述两份 unit、fmr-extraction.slice、/opt/fmr-extractor、/etc/fmr-extractor 与两个专用账号。不要修改其他账号、服务或防火墙。Cloudflare 侧删除专用隧道与 extract-origin.findmyrom.com DNS，并移除网站 EXTRACTOR_ORIGIN / EXTRACTOR_HMAC_SECRET 可关闭入口。

## 验证边界

本机执行网站类型检查、原有回归、新增鉴权/转发/取消测试；服务端使用合成数据测试 HMAC、防重放、忙时拒绝、完整输出、取消清理和损坏流失败行为。Mac 沙箱禁止监听套接字，真实 loopback HTTP 与已有 HTTP 探针交由 Linux runner 检查。真实 VPS cgroup/挂载隔离、连通性和官包交付必须在用户安装后另行验收；部署代码不代表这部分已通过。

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

## 可选的准备进度与提前校验值（schema 3）

`deploy/extractor/update-preparation.sh` 只更新三个 Python 模块并重启 fmr-extractor.service；沿用独立账号、PrivateTmp、挂载隔离和资源限制，不动代理、隧道或凭据。失败自动恢复备份。网站兼容未更新的 schema 2，降级为直接流式下载并隐藏校验值和无法取得的进度。

- POST /v1/prepare：原有完整签名 job，202 开始准备。并发仍为 1；ready 文件也占用该名额，忙时 429。
- POST /v1/job：签名控制 JSON `{id,op,expires}`，op 为 status/download/cancel；HMAC 与原任务相同，90 秒有效。
- status：state、已提取 bytes、可知时 total、平均提取 bytes_per_second；只有完整成功后返回 sha256。
- download：只能领取一次；Content-Length 和 X-FMR-SHA256 确定后原样流式传输。中断/完成关闭临时文件。
- 为满足“完整 SHA-256 先于文件”及原生下载总大小，使用一份最大 256 MiB 的私有匿名临时镜像，绝不缓存整份固件。文件完成后最多保留 5 分钟；准备阶段无轮询 120 秒取消，单任务 480 秒上限。每 5 秒主动清理；进程退出由操作系统释放匿名文件。下载仍按原速率限制，480 秒上限。
- 网站的准备进度轮询不读写 D1；600 秒浏览器票据绑定固件、分区和 job。旧接口仍兼容。
- 网页先绘制完整镜像 SHA-256，再发起文件下载；下载阶段交由浏览器原生管理器呈现大小、速度及进度。总大小尚不确定时显示不定进度，绝不虚构百分比。

## 自动检查并应用更新

一次运行 `deploy/extractor/install-auto-update.sh` 即可安装。它安装独立的 `fmr-extractor-update.service` / `.timer` 和检查器；不把提取服务改成 root，也不改隧道、凭据或代理服务。检查器由 `/opt/fmr-extractor/venv/bin/python` 运行，只用 Python 标准库，不需要新增 pip 包或 GitHub Token。

默认启动后 2 分钟检查，以后每次检查结束约 10 分钟检查一次，附加最多 60 秒随机延迟。安装时也立即执行一次。GitHub 检查固定为 `TunJinshou050608/findmyrom-runner` 的 main 分支和 `deploy/extractor/update-preparation.sh`，不扫描其他仓库或执行其他脚本。使用 ETag；脚本未变化时不会重复下载模块或重启服务。程序文件改动必须同步更新 sh 内的 SHA-256 清单。

发现变更后，脚本与三个模块均固定到脚本变更所在的完整 commit SHA；先验证 shell 语法、模块 SHA-256 和 Python 导入，再备份、安装、重启提取服务并检查本机 capabilities。只有全部成功，才原子保存 `/var/lib/fmr-extractor-update/applied.json`。失败保留此前状态并在下轮重试；更新脚本对安装/健康检查失败自动恢复模块备份。文件锁阻止自动任务及手动更新互相重叠。检查器不自动更新自身；更换检查器或定时配置时重新运行安装脚本。

检查器是独立、短时运行的 root oneshot，以便写入自己的程序目录及重启提取服务；系统文件只读，写入范围限于 `/opt/fmr-extractor`、私有临时目录和自己的状态目录，隐藏提取凭据目录及常见代理/SSH 配置目录。它仍是执行本用户仓库代码的更新通道，应仅由仓库所有者维护。更新会重启提取服务，正在传输的下载可能需要重试。

查看下次检查：`systemctl list-timers --no-pager fmr-extractor-update.timer`

查看日志：`journalctl -u fmr-extractor-update.service -n 50 --no-pager`

立即检查：`systemctl start fmr-extractor-update.service`

暂停自动更新：`systemctl disable --now fmr-extractor-update.timer`

卸载定时器不会停止提取服务。网络失败或 GitHub 限流时仅本次检查失败，现有提取服务继续运行。

## 三星正式链路（2026-11-03）

103 上的三星采集器现在对确切四段版本执行 FUS nonce/Inform 元数据查询，重新排队旧的 requires_samsung_fus 记录。已确认的文件名、路径、大小、完整版本保存在内容寻址描述符中；FUS 明确拒绝的历史版本按 unavailable 退避，不替换为最新版。

D1 仍只由 catalogue_publish.yml 写入。三星描述符与对应签名固件关联存入既有 crawler_sources；网站核对描述符哈希及固件签名后，将完整版本传到 VPS，避免使用用于数据库唯一键的格式化版本。

VPS schema 4 支持三星 partition=full：动态 FUS 初始化、AES 解密并去除 PKCS#7，浏览器直接收到 ZIP。整包不创建临时固件文件；并发仍为 1，沿用输出限速，整包单独限制为 64 GiB / 12 小时。镜像提取的 250 MiB 上游预算、480 秒和准备文件上限保持原值。更新器固定模块哈希，并要求服务报告 samsung_full_stream。

实测记录：Actions 41589174668，在 103 发起普通网站票据。SM-S928B/EUX 的 boot.img 完整交付 100663296 字节，下载 SHA-256 与准备回执相同；普通整包入口返回解密 ZIP，Content-Length=20198138129，读取前 65536 字节后关闭连接。整包未做 20 GB 全量下载，也没有在 103 保存镜像或固件文件。

历史版本验收：Actions 41592059798，SM-A546B/EUX 的 A546BXXSIEZC3 历史构建，boot.img 完整交付 67108864 字节，SHA-256=32a79ba0268e0a6b30a5456af81a52c9f388c9e076236639ac2fb3f2454750a0，与准备回执一致；其 7872655381 字节解密 ZIP 读取前 65536 字节验证成功。该历史版本直接请求确切 FUS 构建，不再被“必须等于当前 FOTA 最新版”的旧限制误拦截。

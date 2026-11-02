# 官包镜像按需流式提取验证（2026-11-02）

本次只增加手动触发的验证流程；官包采集、D1、现有下载入口与镜像仓库任务均未修改。

参考了用户提供的 Samsung、Google、OPLUS、Vivo 四份方案。它们展示了可行的局部读取思路，但示例里的整镜像缓冲/写盘、仅检查 magic、无界 Range 回退和部分协议假设不能直接作为上线依据。

## 可运行的验证程序

- 工作流：`.github/workflows/streaming_extraction_lab.yml`
- 程序：`python -m fmr_extractor.streaming_probe --brand google --output report.json`
- 执行位置：`[self-hosted, Linux, X64, findmyrom-metadata]`，用户容器内现有 runner。
- 每个样本上游读取总预算 250 MiB、480 秒期限；上游忽略 Range 时，在读响应体前关闭连接。
- 接收端只计数并计算哈希，不保存官包和镜像。Actions artifact 仅包含 JSON 指标。
- 每个样本还启动仅监听 `127.0.0.1` 的临时 HTTP 服务，验证实际下载、哈希一致与中途断开；退出即关闭，未开放公网服务。
- Payload 压缩操作最多缓冲 32 MiB；输出块为 256 KiB；Zstd 窗口独立限制为 64 MiB。
- 最后一块输出保留到 CRC/分区哈希检查完成；失败时输出不完整，不能被正常 HTTP 客户端当成成功下载。

## 四类格式的边界

| 来源 | 验证路径 | 完整性依据 | 明确边界 |
|---|---|---|---|
| Google Factory | 外层 ZIP → STORED `image-*.zip` → 单个镜像解压流 | 内层镜像 ZIP CRC32 | 外层嵌套成员必须可随机寻址；不把 Full OTA 当 Factory 处理 |
| OPLUS 全量 OTA | STORED `payload.bin` → Manifest → 目标分区操作 | 每个操作 SHA-256、完整分区 SHA-256 | 源镜像依赖操作拒绝；非连续操作需要进一步实现目标镜像暂存或重排 |
| Vivo 全量 OTA | 同上，按 ZIP 目录定位 Payload | 同上 | 不硬编码 Payload 位于第 0 字节，不以单个样本概括全系 |
| Samsung FUS | FUS 握手/续签 → AES 对齐局部解密 → ZIP/AP TAR/LZ4 | TAR 头校验、LZ4 解码检查、大小与 magic；记录输出 SHA-256 | 提前停止时没有读取完整 AP，不能声称验证了整个 AP 的 CRC 或整包哈希 |

同品牌不同年代、增量/全量、售后专用包不保证同构。检测到未知或不可局部提取的格式时，应明确返回当前包不支持，不能静默下载整个官包。

三星按需提取需要独立下载授权会话；实验实现没有扩大生产 `SmartFUS` 元数据客户端的权限。401 仅重试当前 Range 一次并重新申请授权。AES 的随机块解密不代表内层 Deflate 可直接从任意输出位置恢复。

## 网站接入方式

1. 保持现有官包链接采集方式。下载请求绑定固件的稳定标识、明确版本和分区；短期官包 URL 在服务内部沿用现有解析逻辑刷新，不能悄悄替换成另一个版本。
2. Worker 处理现有下载认证并签发短期提取凭证。凭证绑定固件和分区，后端不接受浏览器随意传入上游 URL。
3. 容器内常驻提取服务验证凭证，按需读取官包局部字节，将原生 `.img` 流直接写入 HTTP 响应；慢客户端通过拉取和写入阻塞限制生产速度，断开则关闭生成器与解码器。
4. 可缓存 ZIP 目录、Payload Manifest 和偏移信息；缓存必须绑定版本及源对象身份。无需把镜像文件保存到 GitHub。
5. 首版不宣称镜像输出支持任意 Range/断点续传。压缩操作重建与输出偏移不是同一个坐标；需要额外索引、重放或仅暂存目标镜像才能实现可靠恢复。
6. Actions 用于离线测试、部署与巡检。公网下载请求由常驻服务处理，不能等待一个新的 Actions 作业启动。

正式切换前仍需验证公网入口、并发/排队、下载凭证、故障恢复，以及当前网站全部拟支持包格式的覆盖情况。当前实验没有部署该公网入口，也没有替换现有镜像下载功能。

## 协议来源

- Android update_engine `update_metadata.proto`：https://android.googlesource.com/platform/system/update_engine/+/refs/heads/main/update_metadata.proto
- Samsung FUS 客户端参考： https://github.com/topjohnwu/samloader-rs （仓库已有 Apache-2.0 归属与许可）
- Zstandard Python C 实现窗口限制： https://github.com/indygreg/python-zstandard/blob/main/c-ext/decompressor.c

## 实测记录

实测数值以关联 Actions 的 JSON 指标为准，不能用参考文档中的性能表替代。首字节时间从分区提取开始计算，不含获取 Google 目录或建立 FUS 会话的前置耗时；实际 HTTP 下载指标单列。各样本仅证明所测具体官包，不代表全系兼容。

全矩阵复测：[Actions 41393766395](https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41393766395)，runner `findmyrom-metadata-103-8`。四类样本全部通过，包含真实 HTTP 输出及断开后生产端停止检查。

| 官包样本 | 官包大小 GiB | 目标 | 镜像 MiB | 上游读取 MiB | 提取耗时 s | 首块输出 s |
|---|---:|---|---:|---:|---:|---:|
| Google Pixel 8a / `akita-ud2a.231203.054` | 2.59 | init_boot | 8 | 1.79 | 2.197 | 1.390 |
| 同上 | 2.59 | boot | 64 | 16.66 | 11.128 | 1.369 |
| OPLUS 文档示例 `233fa60c…zip` | 7.41 | init_boot | 8 | 2.06 | 5.225 | 4.447 |
| 同上 | 7.41 | boot | 192 | 10.47 | 11.932 | 3.197 |
| Vivo Y28s / `PD2357F_EX_A_14.0.16.1.W30.V000L1` | 4.37 | init_boot | 8 | 1.60 | 1.132 | 1.118 |
| 同上 | 4.37 | boot | 96 | 20.40 | 1.729 | 1.375 |
| Samsung SM-S928B / EUX / `S928BXXS6EZJ1` | 18.81 | init_boot | 8 | 85.00 | 19.193 | 18.989 |
| 同上 | 18.81 | boot | 96 | 18.00 | 4.466 | 0.930 |

该轮实际 HTTP 下载耗时：Google init_boot 2.016 秒、OPLUS init_boot 4.176 秒、Vivo init_boot 1.008 秒、Samsung boot 4.888 秒。输出哈希均与相应直接提取结果一致；取消后服务端均确认结束，记录到的额外上游读取为 0。这里的 0 是本轮测量值，不是对所有网络缓冲情况的承诺。

进程峰值 RSS 分别约 51、56、104、65 MiB；官包与镜像均未写盘。运行环境安装依赖、代码检出和 JSON 日志会占用正常测试磁盘空间，这与官包/镜像暂存无关。

复现并修复的问题：

- Google 页面确认 cookie 缺失时没有下载链接，不能误判为官包不存在。
- OPLUS 返回非标准 `Content-Encoding: utf-8`；仅对固定欧加 CDN 容忍该值，按原始二进制读取，仍严格验证 206 与精确 Content-Range。
- Vivo Zstd 流式接口的窗口参数在当前 C 实现中按字节生效；单独检查真实 frame window，避免按 Python 文档中的 KiB 说明设置后错误限制成 64 KiB。
- Samsung 会话在大量细碎 Range 请求期间过期；增加一次性重新授权重试和 1 MiB 解密预读。单个请求仍保留严格预算，不退回整包下载。

欧加样本实际使用 BZip2/XZ/ZERO，Vivo 样本使用 Zstd；因此不能将“欧加全系只用 Zstd”之类描述当作协议保证。Vivo Payload 的实际位置也由中央目录解析，而非依赖文档中的固定偏移。

Google 进一步优化了小 Range 的往返开销：只在 Google 路径启用最多 1 MiB 的有界预读。复测 [Actions 41394127247](https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41394127247) 成功，仍与上表输出哈希相同：

| 目标 | 上游 MiB | 提取 s | 首块 s |
|---|---:|---:|---:|
| init_boot | 2.00 | 1.458 | 1.316 |
| boot | 19.00 | 3.969 | 1.039 |

Google 实际 HTTP init_boot 下载 1.259 秒。整轮实验 Range 请求从 147 降到 71（包括重复提取和两种取消测试），代价是少量额外预读流量。Vivo/OPLUS 保持精确操作读取，不对小操作预读相邻数据。

完整记录归档到原私有临时测试仓库 `findmyrom-tg01-test` 的 `streaming/2026-11-02/`，只包含日志、JSON 指标和说明，不包含官包或镜像二进制。

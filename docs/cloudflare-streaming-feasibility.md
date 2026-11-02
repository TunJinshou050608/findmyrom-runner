# CF 内提取优先：真实边缘验证结果

日期：2026-11-02。优先级按用户决定：①纯 CF 提取；③CF 提供片段、浏览器提取；②浏览器直连官源仅最后考虑。

## 结论

纯 CF 路线已实现独立实验 Worker，并在真实 Cloudflare 边缘运行，不是以 Node 或容器代替 CF。客户端只由用户容器中的 self-hosted runner 运行，哈希后丢弃输出；Mac 没有下载任何官包或镜像。没有写 D1、R2、镜像 GitHub Release，也未修改生产网站 Worker 或现有下载入口。

Google、OPLUS、Vivo 的 boot/init_boot 已有完整输出且 SHA-256 与上一阶段 Python 提取结果一致。Samsung boot 也有完整一致结果，Samsung init_boot 多次触发 CPU 超限；三星 boot 亦出现中途被终止的复测。因此当前账号的免费档不能作为稳定生产方案。

API 直接确认账号处于 Free：上传 `limits.cpu_ms=1000` 被 Cloudflare 以错误 100328 拒绝，提示 CPU limits 不支持 Free。未升级任何订阅或启用收费资源。

## 最近一轮结果

[Actions 41399056562](https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41399056562)，对应实验 Worker 部署 `946c6049b53145d687d5f25edf4b4c83`。此轮 CI 客户端的退出状态为失败，原因包括 CPU 超限导致的截断，不能把部分成功写成整体通过。

| 固定官包样本 | 目标 | 输出 MiB | 完整提取秒数 | 结果 |
|---|---|---:|---:|---|
| Pixel 8a / akita-ud2a.231203.054 | init_boot | 8 | 2.956 | 哈希一致 |
| 同上 | boot | 64 | 6.339 | 哈希一致 |
| OPLUS 文档示例 233fa60c…zip | init_boot | 8 | 9.229 | 操作/分区哈希一致 |
| 同上 | boot | 192 | 23.772 | 操作/分区哈希一致 |
| Vivo Y28s / PD2357F_EX_A_14.0.16.1.W30.V000L1 | init_boot | 8 | 1.196 | 操作/分区哈希一致 |
| 同上 | boot | 96 | 6.497 | 操作/分区哈希一致 |
| Samsung SM-S928B / EUX / S928BXXS6EZJ1 | init_boot | 未完成 | 5.478 后失败 | HTTP 503，1102 CPU 超限 |
| 同上 | boot | 只收到 20 MiB | 未完成 | 截断，哈希不一致 |

三星 boot 在 [Actions 41398206630](https://github.com/TunJinshou050608/findmyrom-runner/actions/runs/41398206630) 成功输出 96 MiB，17.835 秒，SHA-256 `54688aff1c242f6fdd92f80b8d4b96917d7abcef89c6bc0210ce803a6f780e4d`。这证明格式链条可运行，也同时说明免费档单次成功不代表稳定。

Cloudflare 运行日志真实报告过 `exceededCpu`，例如 Vivo boot 1574 ms、三星 init_boot 2010 ms；其他成功请求也消耗上百至上千毫秒 CPU。这里不能用总下载时间代替 CPU 时间。官方免费档保证为每请求 10 ms，偶发超额容忍不是可依赖的容量。

## 已解决的运行时差异

- Google：普通 fetch 路径一度拿到不同的压缩表示，导致 ZIP 偏移错误。使用 CF 自身 `cloudflare:sockets` TLS 请求固定 `Accept-Encoding: identity`，仍在 CF 内执行；没有外部提取服务器。
- OPLUS：非标准 utf-8 Content-Encoding、XZ/BZip2、Wasm 初始化、禁止动态代码生成。解码模块静态上传，移除了 EmBind 的 eval/new Function；请求间不共享可变解码实例。
- Vivo：CF 原生 Zstd 流式接口，限制窗口与操作输入，校验操作和分区 SHA-256。
- Samsung：FUS 元数据会话改用原始 TLS 后授权成功；AES ECB 对齐读取、AP Deflate、TAR、LZ4 逐块提取。实验固定版本并用上一阶段结果的完整镜像 SHA-256 校验。提前停止未校验整个 AP 的 CRC；这不是通用固件目录已经具有镜像 SHA-256 的承诺。
- 所有路径保留最后一块直到完整性检查完成；忽略 Range 的响应在读体前关闭；每次上游读取有 250 MiB 和子请求数量上限。

## 方案一上线前的实际条件

1. 需要用户明确决定是否开通 Workers Paid。官方当前最低月费 $5，含 1000 万请求和 3000 万 CPU 毫秒；不是无限计算。当前没有代为开通。
2. 付费后仍须重测三星 init_boot、大镜像、多并发、慢客户端、取消、源站异常，以及长期失败率；不能仅凭付费额度提高宣称已通过。
3. 现有实验只接受固定样本且令牌限时有效。网站接入需要绑定真实固件标识、固定版本、分区和下载认证；不能开放任意 URL 代理。
4. 保持官包链接采集方式不变；下载时刷新短期 URL 不得变成另一个版本。
5. 不缓存官包/镜像；可以缓存目录、Manifest、偏移与对象身份，避免重复探测。没有必要增加 D1 写入。

## 方案三的备用边界

若不接受 Workers 付费，优先研究 CF 只提供经过认证的必要压缩片段，由浏览器 Web Worker/Wasm 解码、校验并保存。普通 HTTP 片段转发减少服务端解码 CPU；浏览器始终访问 CF，不能偷偷改成方案二的浏览器直连官源。

备用还需要实测浏览器端 Zstd/XZ/BZip2、三星 AES/LZ4、Safari/Android 下载保存、后台挂起与取消。当前没有完成浏览器端矩阵，也没有自动切换为该方案。尤其三星压缩 AP 的扫描和授权不能被一句“转发片段”省略。

## 可复现文件

- `labs/cf-stream/worker.mjs`、`samsung.mjs`、`codecs.mjs`：实验代码。
- `python3 labs/cf-stream/build.py`：从固定 base64 还原静态 Wasm 模块。
- `node --disallow-code-generation-from-strings labs/cf-stream/test-codecs.mjs`：独立 2 MiB 合成数据的解码/哈希与截断测试。
- `.github/workflows/cf_streaming_lab.yml`：self-hosted runner 访问真实 CF，并丢弃镜像二进制，仅保存 JSON。
- 原私有临时仓库 `findmyrom-tg01-test` 的 `streaming/2026-11-02/cloudflare/`：日志和 JSON 证据。

Wrangler dry-run 已验证打包大小约 198 KiB、gzip 约 68 KiB。源代码中的 Wasm 是通用解码器，不是官包或镜像。合成测试不涉及设备数据。

官方文档：

- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/workers/platform/pricing/
- https://developers.cloudflare.com/workers/runtime-apis/sockets/

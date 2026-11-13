# captionreach

活动字幕 HLS 清单和 WebVTT 分片的查看、持续采集、时间轴整理与原文保存。只处理文字，不下载音视频，也不做翻译。

## 现有能力

- `inspect` 查看一份源文件的结构（清单标签、BYTERANGE、WebVTT cue 与 X-TIMESTAMP-MAP）。
- `capture` 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；`show` 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。
- `reach` 持续轮询一份滚动 media playlist，直到 `EXT-X-ENDLIST` 且窗口内没有待补分片，或达到轮询上限。输出按时间排序的字幕、明确标注的缺口、回绕/不连续说明和每段文字的来源证据，可输出 JSON 或中文文字纪要。
- `drill` 启动一场本地脚本化直播（窗口滑动、33 位 MPEGTS 回绕、discontinuity、BYTERANGE 复用、窗口内 500/410、缺映射），在模拟中断（进程重启）前后各打印一次纪要。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m captionreach inspect captionreach/fixtures/live.m3u8
python3 -m captionreach drill
python3 -m captionreach reach http://127.0.0.1:8765/live.m3u8 --minutes
python3 -m unittest discover -s tests -v
python3 -m compileall -q captionreach tests
```

需要手动发请求时运行 `python3 -m captionreach.source_server --port 8765`，再在另一终端运行 `python3 -m captionreach capture http://127.0.0.1:8765/live.m3u8`。默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。持续采集另用 `--state` 指定原子写入的 JSON 状态文件（默认 reach-state.json），重启后从该文件恢复，已确认时间轴不归零、已确认分片不重新抓取。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

## 时间轴与证据规则

- 分片身份 = discontinuity 代次 + media sequence + URI + 字节区间。URI 会复用、同一对象可被 BYTERANGE 切多片，文件名单独不构成身份；清单轮询代次只写进证据，不进入身份。
- WebVTT 的 `X-TIMESTAMP-MAP` 把本地时钟映射到 33 位、90kHz 的 MPEGTS。同一 discontinuity 代次内观察到数值倒退即按 2^33 展开回绕；discontinuity 之后时间轴另起一段，不猜测两边的相邻关系。
- 同一时间、同一文本的 cue 按证据三分类：邻接分片重复携带同一 cue 才合并来源（`overlap`）；同刻不同文本标 `conflict`，两条都保留；同文本但证据不邻接视为真实重复发言（`repeat`/独立条目）。不做全局文本去重，两次“谢谢”不会被合成一次。
- 缺 MPEGTS 映射的分片只保留 WebVTT 本地时间并标注不确定性。
- 已滑出窗口或被源站 410 宣告的分片记永久缺口；仍在窗口内的 5xx/404 等临时失败继续补取。缺口带代次、序列范围、原因和“时间范围不确定”声明，且在收到 ENDLIST 之前就会出现在纪要里。
- 命中 `EXT-X-KEY`、`EXT-X-SESSION-KEY`、`EXT-X-MAP` 等越界标签直接拒绝并说明原因，不下载密钥或初始化片段。

## 模块

`parser.py` 负责单文档观察（含拼接 WebVTT 与 BYTERANGE 续接），`timeline.py` 负责回绕展开、三分类、缺口与不确定性，`collector.py` 负责轮询、分片身份、窗口滑动与原子状态持久化，`archive.py` 保存不可变原文与请求事实，`transport.py` 限制实验请求到本机，`source_server.py` 提供静态来源示例，`drill_server.py` 与 `drill.py` 提供滚动直播演练。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t captionreach:local .` 构建，再执行 `docker run --rm --network none captionreach:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演练通常数秒完成。

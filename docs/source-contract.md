# 字幕清单子集

仅处理 HLS media playlist 和无加密 WebVTT，不获取音视频、master playlist 或密钥。

## 清单与分片

/live.m3u8 初始 media sequence 40，/control/phase?value=slid 滑动到 42 并带 ENDLIST，旧片可 410。EXTINF 是十进制秒；MEDIA-SEQUENCE 给出窗口第一个分片的序号，后续分片序号逐个加一（滚动窗口滑动后序号不重新计算）。DISCONTINUITY-SEQUENCE 提供清单起点所在的不连续代次，DISCONTINUITY 标记其后一个分片进入下一代；不连续点两边不得凭最近时间猜测相邻关系。同一 media sequence 在不同轮询窗口里必须保持稳定的代次归属，采集器在状态文件中维护 sequence→epoch 映射，不随窗口滑动重算。

BYTERANGE 为长度[@起点]，缺起点时沿用同 URI 上一分片的结束位置；不同 URI 不得隐式延续。服务支持单个显式 HTTP Range，并返回 206 和 Content-Range。URI 复用不表示正文/分片相同。分片身份必须结合 discontinuity 代次、media sequence、URI 与字节区间，不能只用文件名；清单轮询代次属于来源证据而非身份。端点为起点包含、起点+长度不包含。未声明支持的 EXT-X-KEY、EXT-X-SESSION-KEY、EXT-X-MAP 等标签必须输出不支持原因并拒绝继续；其余未声明的 EXT-X-* 标签记录为非致命的不支持项。

## WebVTT 与时间轴

WebVTT X-TIMESTAMP-MAP 的 LOCAL 为毫秒精度本地时钟，MPEGTS 是 90kHz 的 33 位计数，时间轴仅在同一 discontinuity 内根据清单顺序延续回绕；同一代次内观察到原始值倒退即按 2^33 累加展开，跨 discontinuity 不展开、另起一段。缺 MPEGTS 映射只能保留本地时间和不确定性。

一个字节对象可拼接多份 WEBVTT 文档（BYTERANGE 切片边界即文档边界），解析时逐文档保留各自的映射头。相邻分片可能重复携带同一 cue：只有映射后区间与文本一致、且证据来自同代次邻接分片时才合并来源（overlap）；相同时间但内容不同记为冲突（conflict），双方都保留；相同文本但证据不邻接视为真实重复发言，各自保留。不做全局文本去重。

## 缺口与结束语义

已经滑出窗口的分片（或仍在窗口内即被源站 410 宣告失效）留下永久缺口；仍在窗口内的临时失败（5xx、404、传输错误）保留为待补取状态并在后续轮询重试，不因单次失败丢失后续字幕。缺口记录包含 discontinuity 代次、media sequence 范围、原因与“时间范围不确定”声明，且在 ENDLIST 到来之前就必须可在纪要中观察到。只有收到 ENDLIST 且窗口内不存在待补取分片时才结束收集；没有 ENDLIST 只表示尚在滚动。采集状态按轮询代次原子落盘，进程重启后从状态恢复，已确认时间轴不归零、已确认分片不重新抓取。词法查看器展示本地 cue 和 URI，不解释完整全局时钟。

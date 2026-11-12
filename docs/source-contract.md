# 字幕清单子集

仅处理 HLS media playlist 和无加密 WebVTT，不获取音视频、master playlist 或密钥。/live.m3u8 初始 media sequence40，/control/phase?value=slid 滑动到42并带ENDLIST，旧片可410。EXTINF 是十进制秒；MEDIA-SEQUENCE对应每个URI顺次加一。DISCONTINUITY-SEQUENCE提供清单起点所在的不连续代次，DISCONTINUITY使后续分片进入下一代；不连续点两边不得凭最近时间猜测相邻关系。

BYTERANGE 为长度[@起点]，缺起点时沿用同URI上一分片的结束位置；不同URI不得隐式延续。服务支持单个显式HTTP Range，并返回206和Content-Range。URI复用不表示正文/分片相同。端点为起点包含、起点+长度不包含。未声明支持的 EXT-X-KEY 等标签必须输出不支持原因。

WebVTT X-TIMESTAMP-MAP 的 LOCAL 为毫秒精度本地时钟，MPEGTS是90kHz的33位计数，时间轴仅在同 discontinuity 内根据清单顺序延续回绕；缺映射只能保留本地时间和不确定性。相邻分片可能重复携带同一cue，只有映射后区间与内容一致且证据属于邻接重叠时才合并来源，不做全局文本去重。没有ENDLIST只表示尚在滚动，窗口外缺片记永久缺口。词法查看器展示本地cue和URI，不解释完整全局时钟。

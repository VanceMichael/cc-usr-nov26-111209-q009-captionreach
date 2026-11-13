"""本地脚本化直播源：一场会滑动、回绕并制造缺口的字幕流。

与 source_server.py 的静态夹具不同，本服务器按“被轮询次数”推进清单，
覆盖持续采集需要的全部麻烦情形：
- 窗口滑动使早期分片 410（永久缺口）；
- EXT-X-DISCONTINUITY 后时间轴另起一段；
- X-TIMESTAMP-MAP 的 MPEGTS 跨越 2^33 回绕；
- 同一 URI 经 BYTERANGE 切出多个分片，相邻分片重复携带同一 cue；
- 窗口内 500 临时失败后恢复，窗口内 410 宣告永久失效；
- 存在缺 MPEGTS 映射的分片（时间不确定性）。
全部内容为合成文字，没有音视频。
"""

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 让 epoch 0 的第二个分片落在回绕线之后。
NEAR_WRAP = (1 << 33) - 90000 * 2  # 距回绕仅 2 秒
WRAPPED_SMALL = 90000              # 回绕后从很小的值重新计数


def _vtt_document(mpegts, cues, local_zero='00:00:00.000'):
    if mpegts is None:
        header = 'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:' + local_zero + '\n\n'
    else:
        header = (f'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:{local_zero},'
                  f'MPEGTS:{mpegts}\n\n')
    body = '\n\n'.join(f'{start} --> {end}\n{text}' for start, end, text in cues)
    return (header + body + '\n').encode('utf-8')


def build_byte_track():
    """拼接多份 WEBVTT 文档，返回 (字节对象, 每个文档的 (offset,length))。

    块索引与 HLS media sequence 是两套编号：同一 URI 被 BYTERANGE 切片复用，
    采集器必须靠字节区间区分身份，而不是靠文件名或块下标。
    """
    documents = [
        # 块 0：序列 40。
        _vtt_document(NEAR_WRAP, [
            ('00:00:00.500', '00:00:01.000', '欢迎参加展会'),
            ('00:00:01.000', '00:00:01.400', '谢谢'),
        ]),
        # 块 1：序列 41，与序列 40 内容完全一致 -> 邻接分片重复携带同一 cue。
        _vtt_document(NEAR_WRAP, [
            ('00:00:00.500', '00:00:01.000', '欢迎参加展会'),
            ('00:00:01.000', '00:00:01.400', '谢谢'),
        ]),
        # 块 2：序列 45。新 discontinuity 代次从很小的基准重新开始（正常递增，
        # 不构成回绕——回绕只在同一代次内数值倒退时才成立）。
        _vtt_document(900000, [
            ('00:00:00.000', '00:00:00.500', '谢谢'),
            ('00:00:00.600', '00:00:01.200', '下一位买家请提问'),
        ]),
        # 块 3：序列 46，缺 MPEGTS 映射，只能保留本地时间。
        _vtt_document(None, [
            ('00:00:00.100', '00:00:00.900', '本场纪要稍后补发'),
        ]),
    ]
    track, ranges = b'', []
    for document in documents:
        ranges.append((len(track), len(document)))
        track += document
    return track, ranges


class DrillServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        self.track, self.ranges = build_byte_track()
        self.polls = 0
        self.segment_attempts = {}
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', port), DrillHandler)

    def playlist_text(self):
        """按轮询代次返回滚动清单，最后一轮带 ENDLIST。"""
        with self.lock:
            self.polls += 1
            poll = self.polls
        ranges = self.ranges

        def block(sequence, index):
            offset, length = ranges[index]
            return (f'#EXTINF:1.5,\n#EXT-X-BYTERANGE:{length}@{offset}\n'
                    f'byte-track.vtt\n')

        if poll == 1:
            text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n'
                    '#EXT-X-MEDIA-SEQUENCE:40\n'
                    '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'
                    + block(40, 0) + block(41, 1)
                    + '#EXTINF:1.5,\nsegment-42.vtt\n')
        elif poll == 2:
            text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n'
                    '#EXT-X-MEDIA-SEQUENCE:41\n'
                    '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'
                    + block(41, 1)
                    + '#EXTINF:1.5,\nsegment-42.vtt\n'
                    + '#EXTINF:1.5,\nsegment-43.vtt\n'
                    + '#EXT-X-DISCONTINUITY\n'
                    + '#EXTINF:1.5,\nsegment-44.vtt\n'
                    + block(45, 2))
        elif poll == 3:
            text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n'
                    '#EXT-X-MEDIA-SEQUENCE:42\n'
                    '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'
                    + '#EXTINF:1.5,\nsegment-42.vtt\n'
                    + '#EXTINF:1.5,\nsegment-43.vtt\n'
                    + '#EXT-X-DISCONTINUITY\n'
                    + '#EXTINF:1.5,\nsegment-44.vtt\n'
                    + block(45, 2) + block(46, 3))
        else:
            text = ('#EXTM3U\n#EXT-X-TARGETDURATION:2\n'
                    '#EXT-X-MEDIA-SEQUENCE:43\n'
                    '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'
                    + '#EXTINF:1.5,\nsegment-43.vtt\n'
                    + '#EXT-X-DISCONTINUITY\n'
                    + '#EXTINF:1.5,\nsegment-44.vtt\n'
                    + block(45, 2) + block(46, 3)
                    + '#EXT-X-ENDLIST\n')
        return text.encode('utf-8')


# 独立分片（非 byte-track）的固定内容。
STANDALONE = {
    '/segment-42.vtt': _vtt_document(WRAPPED_SMALL, [
        ('00:00:00.200', '00:00:00.800', '现在回到主舞台'),
    ]),
    '/segment-43.vtt': _vtt_document(WRAPPED_SMALL, [
        # 与序列 42 同在回绕后、同一展开区间（本地 200-800ms），文本不同
        # -> 同刻冲突，绝不静默覆盖。
        ('00:00:00.200', '00:00:00.800', '欢迎回到下午场'),
    ]),
    '/segment-44.vtt': _vtt_document(600000, [
        ('00:00:00.000', '00:00:00.300', '这段文字注定缺失'),
    ]),
}


class DrillHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *arguments):
        pass

    def do_GET(self):
        target = urllib.parse.urlsplit(self.path)
        path = target.path
        if path == '/live.m3u8':
            body = self.server.playlist_text()
            self._respond(200, 'application/vnd.apple.mpegurl', body)
            return
        if path == '/byte-track.vtt':
            self._serve_track()
            return
        if path in STANDALONE:
            self._serve_standalone(path)
            return
        self._respond(404, 'application/json', b'{"error":"unknown drill route"}')

    def _serve_standalone(self, path):
        server = self.server
        with server.lock:
            attempts = server.segment_attempts.get(path, 0) + 1
            server.segment_attempts[path] = attempts
        if path == '/segment-42.vtt' and attempts == 1:
            self._respond(500, 'text/plain', b'temporary drill failure')
            return
        if path == '/segment-44.vtt':
            # 源站在窗口内即宣告该分片永久不可得。
            self._respond(410, 'text/plain', b'gone within window')
            return
        self._respond(200, 'text/vtt', STANDALONE[path])

    def _serve_track(self):
        server = self.server
        range_header = self.headers.get('Range')
        if not range_header:
            self._respond(400, 'text/plain', b'byte-track requires explicit Range')
            return
        import re
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', range_header)
        if not match:
            self._respond(416, 'text/plain', b'invalid range')
            return
        start, end = int(match[1]), int(match[2])
        if start > end or end >= len(server.track):
            self._respond(416, 'text/plain', b'range out of bounds')
            return
        body = server.track[start:end + 1]
        self.send_response(206)
        self.send_header('Content-Type', 'text/vtt')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(server.track)}')
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

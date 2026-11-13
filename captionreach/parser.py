import re

# 本项目声明支持的 HLS media playlist 标签子集。
SUPPORTED_PLAYLIST_TAGS = {
    '#EXTM3U',
    '#EXT-X-VERSION',
    '#EXT-X-TARGETDURATION',
    '#EXT-X-MEDIA-SEQUENCE',
    '#EXT-X-DISCONTINUITY-SEQUENCE',
    '#EXT-X-DISCONTINUITY',
    '#EXTINF',
    '#EXT-X-BYTERANGE',
    '#EXT-X-ENDLIST',
    '#EXT-X-PLAYLIST-TYPE',
    '#EXT-X-INDEPENDENT-SEGMENTS',
}
# 命中即代表来源超出无加密字幕子集，采集器必须拒绝。
ENCRYPTION_TAGS = {
    '#EXT-X-KEY': '无加密字幕子集不支持 EXT-X-KEY（密钥/加密分片）',
    '#EXT-X-SESSION-KEY': '无加密字幕子集不支持 EXT-X-SESSION-KEY（密钥/加密分片）',
    '#EXT-X-MAP': '无加密字幕子集不支持 EXT-X-MAP（初始化片段，常见于 fMP4）',
}

CLOCK_RE = re.compile(r'(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})')
TIMING_RE = re.compile(r'(.+?) --> (.+?)(?:\s|$)')
MAP_RE = re.compile(r'X-TIMESTAMP-MAP=(.*)')


def parse_clock(value):
    """WebVTT 本地时钟 -> 毫秒。支持 MM:SS.mmm 与 HH:MM:SS.mmm。"""
    match = CLOCK_RE.fullmatch(value.strip())
    if not match:
        raise ValueError('无法解析的 WebVTT 时间: ' + value)
    hours, minutes, seconds, millis = match.groups()
    return ((int(hours or 0)) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(millis)


def parse_timestamp_map(header):
    """解析 X-TIMESTAMP-MAP，LOCAL 缺省为 0，MPEGTS 缺省为 None（时间不确定）。"""
    local_ms, mpegts = 0, None
    for part in header.split(','):
        part = part.strip()
        if part.startswith('LOCAL:'):
            local_ms = parse_clock(part[len('LOCAL:'):])
        elif part.startswith('MPEGTS:'):
            mpegts = int(part[len('MPEGTS:'):])
    return {'local_ms': local_ms, 'mpegts': mpegts, 'raw': header.strip()}


def parse_playlist(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U':
        raise ValueError('缺少 HLS 头')
    sequence = 0
    discontinuity_sequence = None
    target_duration = None
    duration = None
    pending_byterange = None
    pending_discontinuity = False
    entries = []
    ended = False
    unsupported = []
    # 同一清单内每个 URI 最近一次 BYTERANGE 的结束位置，用于缺省起点续接。
    last_end_by_uri = {}
    for line in lines[1:]:
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):
            discontinuity_sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            target_duration = line.split(':', 1)[1]
        elif line == '#EXT-X-DISCONTINUITY':
            pending_discontinuity = True
        elif line.startswith('#EXT-X-BYTERANGE:'):
            spec = line.split(':', 1)[1]
            if '@' in spec:
                length_text, offset_text = spec.split('@', 1)
                pending_byterange = {'length': int(length_text), 'offset': int(offset_text)}
            else:
                # 缺起点：沿用同 URI 上一分片结束位置；不同 URI 不得隐式延续。
                pending_byterange = {'length': int(spec), 'offset': None}
        elif line.startswith('#EXTINF:'):
            duration = line.split(':', 1)[1].split(',')[0]
        elif line == '#EXT-X-ENDLIST':
            ended = True
        elif line.startswith('#'):
            tag = line.split(':', 1)[0]
            if tag in ENCRYPTION_TAGS:
                unsupported.append({'tag': tag, 'reason': ENCRYPTION_TAGS[tag], 'fatal': True})
            elif tag.startswith('#EXT-X-') and tag not in SUPPORTED_PLAYLIST_TAGS:
                unsupported.append({'tag': tag, 'reason': '子集未声明的清单标签，按不支持处理', 'fatal': False})
        else:
            if duration is None:
                raise ValueError('分片缺少 EXTINF')
            byterange = pending_byterange
            if byterange is not None:
                if byterange['offset'] is None:
                    if line not in last_end_by_uri:
                        raise ValueError('BYTERANGE 缺省起点但同 URI 没有前置区间')
                    byterange = {'length': byterange['length'], 'offset': last_end_by_uri[line]}
                last_end_by_uri[line] = byterange['offset'] + byterange['length']
            entries.append({
                'sequence': sequence,
                'uri': line,
                'duration': duration,
                'discontinuity': pending_discontinuity,
                'byterange': byterange,
            })
            sequence += 1
            duration = None
            pending_byterange = None
            pending_discontinuity = False
    if duration is not None:
        raise ValueError('EXTINF 后缺少资源 URI')
    return {
        'kind': 'playlist',
        'media_sequence': sequence,
        'discontinuity_sequence': discontinuity_sequence,
        'target_duration': target_duration,
        'entries': entries,
        'ended': ended,
        'unsupported': unsupported,
    }


def _parse_vtt_document(block, document_index):
    lines = block.splitlines()
    if not lines or not lines[0].startswith('WEBVTT'):
        raise ValueError('不是 WebVTT 文档')
    timing_map = None
    # 文件头行（第一个空行之前）里寻找 X-TIMESTAMP-MAP。
    for header_line in lines[1:]:
        if not header_line.strip():
            break
        match = MAP_RE.search(header_line)
        if match:
            timing_map = parse_timestamp_map(match.group(1))
    cues = []
    for block_text in re.split(r'\n\s*\n', '\n'.join(lines)):
        block_lines = [part for part in block_text.splitlines()]
        for position, cue_line in enumerate(block_lines):
            if ' --> ' not in cue_line:
                continue
            timing = TIMING_RE.fullmatch(cue_line.strip())
            if not timing:
                continue
            start_text, end_text = timing.group(1), timing.group(2)
            cues.append({
                'start': start_text,
                'end': end_text,
                'start_ms': parse_clock(start_text),
                'end_ms': parse_clock(end_text),
                'text': '\n'.join(block_lines[position + 1:]).strip(),
                'document': document_index,
            })
            break
    return {'map': timing_map, 'cues': cues}


def parse_webvtt(text):
    # 同一字节对象可能拼接多份 WEBVTT 文档（BYTERANGE 切片的边界即文档边界）。
    starts = [match.start() for match in re.finditer(r'(?m)^WEBVTT', text)]
    if not starts:
        raise ValueError('不是 WebVTT 文档')
    documents = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        documents.append(_parse_vtt_document(text[start:end].strip(), index))
    cues = []
    for document in documents:
        cues.extend(document['cues'])
    return {'kind': 'vtt', 'documents': documents, 'cues': cues}


def inspect_document(body):
    text = body.decode('utf-8-sig').replace('\r\n', '\n')
    if text.startswith('#EXTM3U'):
        return parse_playlist(text)
    if not text.startswith('WEBVTT'):
        raise ValueError('不是清单或 WebVTT')
    return parse_webvtt(text)

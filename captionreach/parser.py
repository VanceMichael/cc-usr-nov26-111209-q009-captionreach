import re


def parse_playlist(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U':
        raise ValueError('缺少 HLS 头')
    sequence, duration, entries, ended = 0, None, [], False
    for line in lines[1:]:
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXTINF:'):
            duration = line.split(':', 1)[1].split(',')[0]
        elif line == '#EXT-X-ENDLIST':
            ended = True
        elif not line.startswith('#'):
            if duration is None:
                raise ValueError('分片缺少 EXTINF')
            entries.append({'sequence': sequence + len(entries), 'uri': line, 'duration': duration})
            duration = None
    if duration is not None:
        raise ValueError('EXTINF 后缺少资源 URI')
    return {'kind': 'playlist', 'entries': entries, 'ended': ended}


def inspect_document(body):
    text = body.decode('utf-8-sig').replace('\r\n', '\n')
    if text.startswith('#EXTM3U'):
        return parse_playlist(text)
    if not text.startswith('WEBVTT'):
        raise ValueError('不是清单或 WebVTT')
    cues = []
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        for position, line in enumerate(lines):
            if ' --> ' in line:
                start, stop = line.split(' --> ', 1)
                cues.append({'start': start, 'end': stop, 'text': '\n'.join(lines[position + 1:])})
                break
    return {'kind': 'vtt', 'cues': cues}

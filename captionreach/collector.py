"""持续采集器：轮询滚动 HLS 清单，只取文字分片，状态可恢复。

关键约束：
- 分片身份 = 清单代次 + media sequence + URI + 字节区间 + discontinuity 代次，
  URI 复用或 BYTERANGE 切同一对象都不会被当作同一个分片；
- 已滑出窗口的分片记永久缺口，不阻塞后续字幕；窗口内临时失败继续补取；
- 每个轮询代次原子落盘，重启后时间轴不归零、已确认分片不重复抓取；
- 见到 ENDLIST 且窗口内没有待补分片，才结束收集。
"""

import json
import os
import tempfile
import urllib.error
from pathlib import Path

from .parser import inspect_document
from .timeline import build_timeline, render_minutes
from .transport import fetch, fetch_range

STATE_VERSION = 1
TRANSIENT_STATUSES = {404, 408, 425, 500, 502, 503, 504}


class _PlaylistUnavailable(Exception):
    """清单轮询临时失败：不改变任何分片结论，下一轮继续。"""


def segment_identity(entry, epoch):
    """分片的稳定身份：不连续代次 + media sequence + URI + 字节区间。

    URI 会复用，文件名单独不构成身份；清单轮询代次只属于“证据”，
    不进入身份，否则同一分片每轮都会被当成新分片重新抓取。
    """
    byterange = entry.get('byterange')
    range_text = None
    if byterange:
        range_text = f"{byterange['offset']}:{byterange['length']}"
    return '|'.join([
        'ep' + str(epoch),
        'seq' + str(entry['sequence']),
        entry['uri'],
        range_text or 'full',
    ])


class ReachStore:
    """原子持久化的采集状态。重启只读取事实，不重建时间轴。"""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state.get('version') != STATE_VERSION:
                raise ValueError('状态文件版本不兼容')
        else:
            self.state = {
                'version': STATE_VERSION,
                'playlist_url': None,
                'generation': 0,
                'ended': False,
                'initial_discontinuity_sequence': None,
                'segments': {},
                'playlists': [],
                'unsupported': [],
            }
            self.flush()

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.reach-', suffix='.json', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def segment(self, identity):
        return self.state['segments'].get(identity)

    def all_segments(self):
        return list(self.state['segments'].values())


class Collector:
    def __init__(self, playlist_url, store, archive, sleep=lambda seconds: None):
        self.playlist_url = playlist_url
        self.store = store
        self.archive = archive
        self.sleep = sleep
        if self.store.state['playlist_url'] is None:
            self.store.state['playlist_url'] = playlist_url
            self.store.flush()
        elif self.store.state['playlist_url'] != playlist_url:
            raise ValueError('状态文件属于另一个清单地址')

    def poll_once(self):
        """执行一次清单轮询并抓取窗口内的新分片/待补分片。"""
        state = self.store.state
        try:
            status, content_type, body = self._fetch_playlist()
        except _PlaylistUnavailable as unavailable:
            # 清单本身暂时拿不到：不产生任何缺口结论，下一轮再试。
            self.store.flush()
            return {'generation': state['generation'], 'playlist_status': None,
                    'error': unavailable.args[0], 'fetched': [], 'gap_closed': []}
        state['generation'] += 1
        generation = state['generation']
        playlist_record = {
            'generation': generation,
            'http_status': status,
            'content_type': content_type,
            'digest_note': '清单原文经 archive 保存' if 200 <= status < 300 else '错误响应未作为分片原文',
        }
        if not 200 <= status < 300:
            playlist_record['error'] = f'清单 HTTP {status}'
            state['playlists'].append(playlist_record)
            self.store.flush()
            return {'generation': generation, 'playlist_status': status, 'fetched': [], 'gap_closed': []}
        self.archive.save(self.playlist_url + '#playlist-gen' + str(generation),
                          status, content_type, body)
        playlist = inspect_document(body)
        playlist_record['media_sequence'] = playlist['media_sequence']
        playlist_record['entry_sequences'] = [entry['sequence'] for entry in playlist['entries']]
        state['playlists'].append(playlist_record)

        fatal = [item for item in playlist['unsupported'] if item.get('fatal')]
        if fatal:
            # 加密等越界内容：明确拒绝，不猜测、不下载密钥。
            state['unsupported'].extend({'generation': generation, **item} for item in fatal)
            self.store.flush()
            raise ValueError('不支持的来源标签: ' + '; '.join(item['tag'] for item in fatal))
        if playlist['unsupported']:
            state['unsupported'].extend({'generation': generation, **item}
                                        for item in playlist['unsupported'])

        if state['initial_discontinuity_sequence'] is None:
            state['initial_discontinuity_sequence'] = playlist['discontinuity_sequence']
        epochs = list(self._epoch_cursor(playlist))
        window_sequences = [entry['sequence'] for entry in playlist['entries']]
        window_min = min(window_sequences) if window_sequences else None
        # 本代清单每个 discontinuity 代次可见的序列区间，供滑出判定使用。
        self._visible_epoch_sequences = {}
        for entry, epoch in zip(playlist['entries'], epochs):
            low, high = self._visible_epoch_sequences.get(
                epoch, (entry['sequence'], entry['sequence']))
            self._visible_epoch_sequences[epoch] = (
                min(low, entry['sequence']), max(high, entry['sequence']))

        self._mark_slid_out(generation)
        # 窗口起点之前、本地从未见过的序列无法可靠归属代次/URI，不臆造记录；
        # 其缺失由 timeline 按代次的可见序列边界如实标注为永久缺口。

        fetched, gap_closed = [], []
        for entry, epoch in zip(playlist['entries'], epochs):
            identity = segment_identity(entry, epoch)
            known = self.store.segment(identity)
            if known and known['status'] == 'captured':
                known['last_seen_generation'] = generation
                continue
            result = self._fetch_segment(entry, epoch, generation, identity,
                                         in_window=True, window_min=window_min)
            if result == 'captured':
                fetched.append(identity)
            if known and known['status'] != 'captured' and result == 'captured':
                gap_closed.append(identity)

        state['ended'] = playlist['ended']
        self.store.flush()
        return {
            'generation': generation,
            'playlist_status': status,
            'ended': playlist['ended'],
            'fetched': fetched,
            'gap_closed': gap_closed,
            'unsupported': playlist['unsupported'],
        }

    def run(self, max_polls=None, interval_seconds=1.0):
        polls = 0
        while True:
            result = self.poll_once()
            polls += 1
            if self._finished():
                return result
            if max_polls is not None and polls >= max_polls:
                return result
            self.sleep(interval_seconds)

    def _finished(self):
        state = self.store.state
        if not state['ended']:
            return False
        return all(segment['status'] != 'pending_retry' for segment in state['segments'].values())

    def _fetch_playlist(self):
        try:
            return fetch(self.playlist_url)
        except (urllib.error.URLError, OSError, ValueError) as error:
            # 传输失败不伪造 HTTP 状态，交由下一轮重试；状态文件如实记录。
            state = self.store.state
            state['generation'] += 1
            state['playlists'].append({
                'generation': state['generation'],
                'http_status': None,
                'error': type(error).__name__ + ': ' + str(error),
            })
            raise _PlaylistUnavailable(type(error).__name__ + ': ' + str(error))

    def _epoch_cursor(self, playlist):
        """给每个 entry 分配跨轮询稳定的 discontinuity 代次。

        已记录的 media sequence 沿用其既有代次，保证同一分片不会因窗口滑动
        而换代；新序列沿清单顺序，在 discontinuity 标记处进入下一代。
        """
        state = self.store.state
        if 'epoch_by_sequence' not in state:
            state['epoch_by_sequence'] = {}
        epoch_map = state['epoch_by_sequence']
        base = state['initial_discontinuity_sequence'] or 0
        current = base
        for entry in playlist['entries']:
            if entry['discontinuity']:
                current += 1
            sequence = entry['sequence']
            if sequence in epoch_map:
                current = epoch_map[sequence]
            else:
                epoch_map[sequence] = current
            yield current

    def _mark_slid_out(self, generation):
        """已记录但不再出现、且已低于代次窗口起点的分片：永久缺口，不再等待。

        只在同一 discontinuity 代次内比较序列，避免跨代次误判。
        """
        visible_epochs = self._visible_epoch_sequences
        if not visible_epochs:
            return
        for segment in self.store.state['segments'].values():
            if segment['status'] == 'captured':
                continue
            visible = visible_epochs.get(segment['epoch'], (None, None))
            if visible[0] is None:
                continue
            if visible[0] <= segment['sequence'] <= visible[1]:
                continue  # 仍在窗口内：保持临时缺口，后续补取
            if segment['sequence'] < visible[0]:
                segment['status'] = 'permanent_gap'
                segment['reason'] = segment.get('reason') or '分片已滑出滚动窗口'
                segment['closed_generation'] = generation

    def _fetch_segment(self, entry, epoch, generation, identity, in_window, window_min):
        url = self._segment_url(entry)
        headers_range = None
        try:
            if entry.get('byterange'):
                byterange = entry['byterange']
                start, end = byterange['offset'], byterange['offset'] + byterange['length'] - 1
                status, content_type, body = self._fetch_with_range(url, start, end)
                headers_range = f'bytes={start}-{end}'
            else:
                status, content_type, body = fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as error:
            self._record_failure(identity, entry, epoch, generation, None,
                                 '网络/传输失败: ' + type(error).__name__, in_window, window_min)
            return 'pending_retry'

        if not 200 <= status < 300:
            reason = f'HTTP {status}'
            if status == 410:
                # 源站明确宣告分片永久不可得（无论是否仍在窗口），不再补取。
                self._record_failure(identity, entry, epoch, generation, status,
                                     reason + ' 源站宣告分片失效', in_window=False,
                                     window_min=window_min)
                return 'permanent_gap'
            # 5xx/404 等：仍在窗口内则临时补取，已滑出则永久缺口。
            self._record_failure(identity, entry, epoch, generation, status,
                                 reason, in_window, window_min)
            if status in TRANSIENT_STATUSES:
                return 'pending_retry'
            self.store.segment(identity)['status'] = 'permanent_gap'
            self.store.segment(identity)['reason'] = reason + ' 分片已不在窗口'
            return 'permanent_gap'

        return self._store_captured(identity, entry, epoch, generation, url,
                                    status, content_type, body, headers_range)

    def _store_captured(self, identity, entry, epoch, generation, url,
                        status, content_type, body, headers_range):
        parsed = inspect_document(body)
        if parsed['kind'] != 'vtt':
            self._record_failure(identity, entry, epoch, generation, status,
                                 '分片不是 WebVTT', True, None)
            return 'pending_retry'
        capture_id = self.archive.save(url + ('#' + headers_range if headers_range else ''),
                                       status, content_type, body)
        documents = parsed['documents']
        primary_map = documents[0]['map'] if documents else None
        mpegts = primary_map['mpegts'] if primary_map else None
        local_zero = primary_map['local_ms'] if primary_map else 0
        record = {
            'identity': identity,
            'epoch': epoch,
            'sequence': entry['sequence'],
            'uri': entry['uri'],
            'byterange': entry.get('byterange'),
            'first_seen_generation': generation,
            'last_seen_generation': generation,
            'status': 'captured',
            'http_status': status,
            'capture_id': capture_id,
            'digest': self._digest_of(capture_id),
            'mpegts': mpegts,
            'local_zero': local_zero,
            'cues': parsed['cues'],
            'map_raw': primary_map['raw'] if primary_map else None,
            'document_count': len(documents),
            'reason': None,
        }
        self.store.state['segments'][identity] = record
        return 'captured'

    def _record_failure(self, identity, entry, epoch, generation, status, reason,
                        in_window, window_min):
        permanent = (not in_window) or (
            window_min is not None and entry['sequence'] < window_min)
        existing = self.store.segment(identity)
        record = existing or {
            'identity': identity,
            'epoch': epoch,
            'sequence': entry['sequence'],
            'uri': entry['uri'],
            'byterange': entry.get('byterange'),
            'first_seen_generation': generation,
            'cues': [],
        }
        record.update({
            'epoch': epoch,
            'last_seen_generation': generation,
            'status': 'permanent_gap' if permanent else 'pending_retry',
            'http_status': status,
            'reason': reason,
        })
        self.store.state['segments'][identity] = record

    def _segment_url(self, entry):
        base = self.playlist_url.rsplit('/', 1)[0] + '/'
        return base + entry['uri']

    def _fetch_with_range(self, url, start, end):
        return fetch_range(url, start, end)

    def _digest_of(self, capture_id):
        row = next((item for item in self.archive.rows() if item['id'] == capture_id), None)
        return row['digest'] if row else None

    def report(self):
        report = build_timeline(self.store.all_segments())
        report['state'] = {
            'playlist_url': self.store.state['playlist_url'],
            'generation': self.store.state['generation'],
            'ended': self.store.state['ended'],
            'segment_count': len(self.store.state['segments']),
            'unsupported': self.store.state['unsupported'],
        }
        return report

    def minutes(self):
        return render_minutes(self.report())

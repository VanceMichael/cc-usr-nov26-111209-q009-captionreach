"""可解释的字幕时间轴。

输入采集器持久化的分片状态（含已解析 cue 与原文证据），输出按时间排序的
说话内容、缺口、不连续边界与时间不确定性。这里不做全局文本去重：
两次“谢谢”只要不是邻接分片携带的同一 cue，就各自保留。
"""

MPEGTS_WRAP = 1 << 33  # 33 位 MPEGTS 回绕点
TICKS_PER_MS = 90


def unwrap_epoch_bases(segments):
    """在同一 discontinuity 代次内展开 33 位 MPEGTS 回绕。

    返回 {(epoch, sequence): 展开后的基准 ticks}。原始 33 位值同时保留，
    只对回绕后的分片累加整圈。缺 MPEGTS 映射的分片不参与展开。
    """
    bases = {}
    raw_bases = {}
    current_epoch = None
    running_base = 0
    previous_raw = None
    for segment in sorted(segments, key=lambda item: (item['epoch'], item['sequence'])):
        if segment['epoch'] != current_epoch:
            current_epoch = segment['epoch']
            running_base = 0
            previous_raw = None
        raw = segment.get('mpegts')
        key = (segment['epoch'], segment['sequence'])
        if raw is None:
            continue
        if previous_raw is not None and raw < previous_raw:
            # 本地观察到回绕：后续基准整体抬升一圈，而不是把旧值改大。
            running_base += MPEGTS_WRAP
        bases[key] = running_base + raw
        raw_bases[key] = raw
        previous_raw = raw
    return bases, raw_bases


def _cue_interval(segment, cue, bases):
    key = (segment['epoch'], segment['sequence'])
    mpegts = segment.get('mpegts')
    local_offset_ms = cue['start_ms'] - segment.get('local_zero', 0)
    local_offset_end_ms = cue['end_ms'] - segment.get('local_zero', 0)
    if mpegts is None or key not in bases:
        # 缺映射：只能保留本地时间，明确标记时间不确定性。
        return {
            'mapped': False,
            'local_start_ms': cue['start_ms'],
            'local_end_ms': cue['end_ms'],
            'start_ticks': None,
            'end_ticks': None,
        }
    start_ticks = bases[key] + local_offset_ms * TICKS_PER_MS
    end_ticks = bases[key] + local_offset_end_ms * TICKS_PER_MS
    return {
        'mapped': True,
        'local_start_ms': cue['start_ms'],
        'local_end_ms': cue['end_ms'],
        'start_ticks': start_ticks,
        'end_ticks': end_ticks,
    }


def _group_key(interval):
    if interval['mapped']:
        return ('mapped', interval['start_ticks'], interval['end_ticks'])
    return ('local', interval['local_start_ms'], interval['local_end_ms'])


def _evidence(segment, cue):
    return {
        'epoch': segment['epoch'],
        'sequence': segment['sequence'],
        'uri': segment['uri'],
        'byterange': segment['byterange'],
        'first_seen_generation': segment.get('first_seen_generation'),
        'last_seen_generation': segment.get('last_seen_generation'),
        'capture_id': segment.get('capture_id'),
        'digest': segment.get('digest'),
        'http_status': segment.get('http_status'),
        'document': cue.get('document', 0),
        'local_start': cue['start'],
        'local_end': cue['end'],
    }


def _classify(group):
    """同一时间键、同一文本的 cue 集合按证据关系分类，绝不按文本全局去重。"""
    if len(group) == 1:
        return 'single'
    ordered = sorted(group, key=lambda item: (item['segment']['epoch'], item['segment']['sequence']))
    adjacent = all(
        ordered[index + 1]['segment']['sequence'] == ordered[index]['segment']['sequence'] + 1
        and ordered[index + 1]['segment']['epoch'] == ordered[index]['segment']['epoch']
        for index in range(len(ordered) - 1)
    )
    if adjacent:
        # 邻接分片在同一映射区间重复携带同一 cue，才是可合并的重叠副本。
        return 'overlap'
    return 'repeat'  # 同文本同区间但证据不邻接：真正重复发言，各自保留


def build_timeline(segments):
    captured = [segment for segment in segments if segment['status'] == 'captured']
    bases, raw_bases = unwrap_epoch_bases(captured)

    events_raw = []
    for segment in captured:
        for cue in segment.get('cues', []):
            interval = _cue_interval(segment, cue, bases)
            events_raw.append({
                'epoch': segment['epoch'],
                'sequence': segment['sequence'],
                'segment': segment,
                'cue': cue,
                'interval': interval,
                'evidence': _evidence(segment, cue),
            })

    def event_sort_key(item):
        interval = item['interval']
        if interval['mapped']:
            return (0, item['epoch'], interval['start_ticks'], item['sequence'], item['cue'].get('document', 0))
        # 无映射事件只能在本地时间维度排序，排在同代映射事件之后并标注。
        return (1, item['epoch'], interval['local_start_ms'], item['sequence'], item['cue'].get('document', 0))

    events_raw.sort(key=event_sort_key)

    # 先按时间键聚合全部 cue，再在桶内按文本区分：
    # 同文本 -> 看证据是否邻接（重叠副本 vs 真实重复）；不同文本 -> 同刻冲突。
    buckets = {}
    for item in events_raw:
        buckets.setdefault((item['epoch'], _group_key(item['interval'])), []).append(item)

    timeline = []
    for (epoch, _timing_key), items in buckets.items():
        text_groups = {}
        for item in items:
            text_groups.setdefault(item['cue']['text'], []).append(item)
        for text, group_items in text_groups.items():
            classification = 'conflict' if len(text_groups) > 1 else _classify(group_items)
            first = group_items[0]
            interval = first['interval']
            timeline.append({
                'epoch': epoch,
                'classification': classification,
                'text': text,
                'timing': {
                    'mapped': interval['mapped'],
                    'start_ticks': interval['start_ticks'],
                    'end_ticks': interval['end_ticks'],
                    'local_start_ms': interval['local_start_ms'],
                    'local_end_ms': interval['local_end_ms'],
                    'start_local': first['cue']['start'],
                    'end_local': first['cue']['end'],
                },
                'sources': [item['evidence'] for item in group_items],
                'sequence_range': [
                    min(item['sequence'] for item in group_items),
                    max(item['sequence'] for item in group_items),
                ],
                'conflicting_texts': sorted(other for other in text_groups if other != text)
                if len(text_groups) > 1 else [],
            })

    timeline.sort(key=lambda event: (
        0 if event['timing']['mapped'] else 1,
        event['epoch'],
        event['timing']['start_ticks'] if event['timing']['mapped'] else event['timing']['local_start_ms'],
        event['sequence_range'][0],
    ))

    epochs = _build_epochs(segments)
    gaps = _build_gaps(segments)
    return {
        'timeline': timeline,
        'gaps': gaps,
        'epochs': epochs,
        'wrap_points': _build_wrap_points(captured, raw_bases),
        'uncertainty': _build_uncertainty(captured, segments),
    }


def _build_epochs(segments):
    epochs = {}
    for segment in segments:
        info = epochs.setdefault(segment['epoch'], {
            'epoch': segment['epoch'],
            'min_sequence': segment['sequence'],
            'max_sequence': segment['sequence'],
            'has_mapping': segment.get('mpegts') is not None,
        })
        info['min_sequence'] = min(info['min_sequence'], segment['sequence'])
        info['max_sequence'] = max(info['max_sequence'], segment['sequence'])
        if segment.get('mpegts') is not None:
            info['has_mapping'] = True
    return [epochs[key] for key in sorted(epochs)]


def _build_gaps(segments):
    """缺口只来自两种事实：滑出窗口的永久缺口，或仍待补取的临时缺口。

    按代次把已知分片排序，缺失的 sequence（含滑出前从未见过的序列）
    合并成连续区间，永久/临时属性与原因逐条如实列出。
    """
    by_epoch = {}
    for segment in segments:
        by_epoch.setdefault(segment['epoch'], []).append(segment)
    gaps = []
    for epoch in sorted(by_epoch):
        members = sorted(by_epoch[epoch], key=lambda item: item['sequence'])
        by_sequence = {item['sequence']: item for item in members}
        minimum = members[0]['sequence']
        maximum = members[-1]['sequence']
        run = []

        def flush(run):
            if not run:
                return
            permanent = all(item['status'] == 'permanent_gap' for item in run)
            reasons = sorted({item.get('reason') or '未知原因' for item in run})
            if maximum == run[-1]['sequence']:
                note = '窗口尾部缺失，时间范围无法由后继分片界定'
            elif not permanent:
                note = '分片仍在窗口内，后续轮询继续补取'
            elif any('410' in reason for reason in reasons):
                note = '源站宣告分片永久失效，字幕永久缺失'
            elif any('滑出' in reason for reason in reasons):
                note = '分片已滑出清单窗口，字幕永久缺失'
            else:
                note = '分片永久不可得，字幕缺失'
            gaps.append({
                'epoch': epoch,
                'from_sequence': run[0]['sequence'],
                'to_sequence': run[-1]['sequence'],
                'kind': 'permanent' if permanent else 'pending_retry',
                'reasons': reasons,
                'time_uncertain': True,
                'note': note,
            })

        for sequence in range(minimum, maximum + 1):
            item = by_sequence.get(sequence)
            if item is None or item['status'] != 'captured':
                if item is None:
                    item = {
                        'epoch': epoch,
                        'sequence': sequence,
                        'status': 'permanent_gap',
                        'reason': '分片在首次观察前已滑出窗口，无法补取',
                    }
                run.append(item)
            else:
                flush(run)
                run = []
        flush(run)
    return gaps


def _build_wrap_points(captured, raw_bases):
    points = []
    previous = None
    for segment in sorted(captured, key=lambda item: (item['epoch'], item['sequence'])):
        raw = raw_bases.get((segment['epoch'], segment['sequence']))
        if raw is None:
            continue
        if previous is not None and previous['epoch'] == segment['epoch'] \
                and raw < previous['raw']:
            # 33 位计数器只在数值倒退时回绕；同代次内正常递增不产生回绕点。
            points.append({
                'epoch': segment['epoch'],
                'after_sequence': previous['sequence'],
                'at_sequence': segment['sequence'],
                'raw_before': previous['raw'],
                'raw_after': raw,
                'note': '33 位 MPEGTS 回绕，时间轴在此累加 2^33 ticks，不改变原文顺序',
            })
        previous = {'epoch': segment['epoch'], 'sequence': segment['sequence'], 'raw': raw}
    return points


def _build_uncertainty(captured, segments):
    unmapped = sorted(
        ({'epoch': item['epoch'], 'sequence': item['sequence']}
         for item in captured if item.get('mpegts') is None),
        key=lambda item: (item['epoch'], item['sequence']),
    )
    return {
        'segments_without_mpegts_map': unmapped,
        'gap_time_ranges_uncertain': True,
        'note': '缺 X-TIMESTAMP-MAP 的分片仅保留 WebVTT 本地时间；缺口没有原文，时间范围只能按相邻分片估计',
    }


def _format_ticks(ticks):
    if ticks is None:
        return '本地时间(无全局映射)'
    seconds = ticks / 90000
    return f'{seconds:9.3f}s (ticks={ticks})'


def render_minutes(report):
    """打印给运营同事的中文文字纪要：缺口与不确定性必须可见。"""
    lines = []
    lines.append('字幕时间轴纪要（仅文字，无音视频；时间为 MPEGTS 90kHz 映射后的代次内秒数）')
    lines.append('=' * 72)
    labels = {
        'single': '发言',
        'overlap': '重叠副本已合并来源',
        'conflict': '同刻内容冲突',
        'repeat': '重复发言(各自保留)',
    }
    for event in report['timeline']:
        timing = event['timing']
        when = _format_ticks(timing['start_ticks'])
        marker = labels[event['classification']]
        conflict = ''
        if event.get('conflicting_texts'):
            conflict = '；同刻另有内容: ' + ' / '.join(event['conflicting_texts'])
        lines.append(f"[代次{event['epoch']}] {when} {marker}: {event['text']}{conflict}")
        for source in event['sources']:
            range_text = ''
            if source['byterange']:
                range_text = f" bytes={source['byterange']['offset']}-{source['byterange']['offset'] + source['byterange']['length'] - 1}"
            digest = (source['digest'] or '无原文')[:12]
            lines.append(
                f"    证据: 序列{source['sequence']} {source['uri']}{range_text} "
                f"首见清单代次{source['first_seen_generation']} 抓取#{source['capture_id']} 摘要{digest}"
            )
    lines.append('')
    lines.append('缺口声明')
    lines.append('-' * 72)
    if not report['gaps']:
        lines.append('无缺口。')
    for gap in report['gaps']:
        kind = '永久缺口' if gap['kind'] == 'permanent' else '临时缺口(继续补取)'
        lines.append(
            f"[代次{gap['epoch']}] 序列 {gap['from_sequence']}-{gap['to_sequence']} {kind}："
            f"{gap['note']}；原因 {', '.join(gap['reasons'])}；时间范围不确定"
        )
    lines.append('')
    lines.append('时间轴说明')
    lines.append('-' * 72)
    for point in report['wrap_points']:
        lines.append(
            f"[代次{point['epoch']}] 序列{point['at_sequence']} 处 MPEGTS 回绕 "
            f"({point['raw_before']} -> {point['raw_after']})，已按顺序展开"
        )
    unmapped = report['uncertainty']['segments_without_mpegts_map']
    if unmapped:
        lines.append('以下分片缺 MPEGTS 映射，仅保留本地时间：' +
                     ', '.join(f"代次{item['epoch']}#序列{item['sequence']}" for item in unmapped))
    lines.append(report['uncertainty']['note'])
    return '\n'.join(lines)

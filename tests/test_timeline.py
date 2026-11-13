import unittest

from captionreach.timeline import MPEGTS_WRAP, build_timeline, render_minutes


def segment(epoch, sequence, mpegts, cues, uri='s.vtt', byterange=None,
            status='captured', reason=None):
    return {
        'identity': f'ep{epoch}|seq{sequence}',
        'epoch': epoch,
        'sequence': sequence,
        'uri': uri,
        'byterange': byterange,
        'first_seen_generation': 1,
        'last_seen_generation': 1,
        'status': status,
        'http_status': 200 if status == 'captured' else 410,
        'capture_id': sequence + 1,
        'digest': 'deadbeef' * 8,
        'mpegts': mpegts,
        'local_zero': 0,
        'cues': [
            {'start': s, 'end': e, 'start_ms': sm, 'end_ms': em, 'text': text, 'document': 0}
            for (s, e, sm, em, text) in cues
        ],
        'reason': reason,
    }


CUE = lambda sm, em, text: ('00:00:00.000', '00:00:00.000', sm, em, text)


class TimelineTests(unittest.TestCase):
    def test_adjacent_overlap_merges_sources_but_keeps_one_event(self):
        shared_cues = [CUE(500, 1000, '欢迎'), CUE(1000, 1400, '谢谢')]
        segments = [
            segment(0, 40, 1000, shared_cues, byterange={'offset': 0, 'length': 10}),
            segment(0, 41, 1000, shared_cues, byterange={'offset': 10, 'length': 10}),
        ]
        report = build_timeline(segments)
        events = report['timeline']
        self.assertEqual([event['classification'] for event in events], ['overlap', 'overlap'])
        welcome = next(event for event in events if event['text'] == '欢迎')
        self.assertEqual([source['sequence'] for source in welcome['sources']], [40, 41])

    def test_same_text_different_time_is_real_repeat(self):
        segments = [
            segment(0, 40, 1000, [CUE(0, 500, '谢谢')]),
            segment(0, 45, 1000, [CUE(5000, 5500, '谢谢')]),
        ]
        report = build_timeline(segments)
        self.assertEqual(len(report['timeline']), 2)
        self.assertTrue(all(event['classification'] == 'single' for event in report['timeline']))
        self.assertEqual([event['text'] for event in report['timeline']], ['谢谢', '谢谢'])

    def test_same_time_different_text_is_conflict(self):
        segments = [
            segment(0, 42, 1000, [CUE(200, 800, '现在回到主舞台')]),
            segment(0, 43, 1000, [CUE(200, 800, '欢迎回到下午场')]),
        ]
        report = build_timeline(segments)
        self.assertEqual({event['classification'] for event in report['timeline']}, {'conflict'})
        texts = {event['text'] for event in report['timeline']}
        self.assertEqual(texts, {'现在回到主舞台', '欢迎回到下午场'})

    def test_mpegts_33bit_wrap_unwraps_within_epoch(self):
        near = MPEGTS_WRAP - 90000 * 2
        segments = [
            segment(0, 40, near, [CUE(0, 100, '回绕前')]),
            segment(0, 41, 90000, [CUE(0, 100, '回绕后')]),
        ]
        report = build_timeline(segments)
        self.assertEqual(len(report['wrap_points']), 1)
        point = report['wrap_points'][0]
        self.assertEqual(point['at_sequence'], 41)
        # 回绕后的事件展开 ticks 必须大于回绕前事件。
        before, after = report['timeline']
        self.assertLess(before['timing']['start_ticks'], after['timing']['start_ticks'])

    def test_discontinuity_starts_fresh_axis_no_false_wrap(self):
        # 代次切换即便数值变小也不是回绕。
        segments = [
            segment(0, 40, MPEGTS_WRAP - 1000, [CUE(0, 100, '上代次')]),
            segment(1, 41, 600000, [CUE(0, 100, '新代次')]),
        ]
        report = build_timeline(segments)
        self.assertEqual(report['wrap_points'], [])
        self.assertEqual([event['epoch'] for event in report['timeline']], [0, 1])

    def test_permanent_and_pending_gaps_listed_with_uncertainty(self):
        segments = [
            segment(0, 40, 1000, [CUE(0, 100, '甲')]),
            segment(0, 41, None, [], status='permanent_gap', reason='HTTP 410 源站宣告分片失效'),
            segment(0, 42, None, [], status='pending_retry', reason='HTTP 500'),
            segment(0, 43, 1000, [CUE(0, 100, '乙')]),
        ]
        report = build_timeline(segments)
        self.assertEqual(len(report['gaps']), 1)
        gap = report['gaps'][0]
        self.assertEqual((gap['from_sequence'], gap['to_sequence']), (41, 42))
        self.assertTrue(gap['time_uncertain'])
        self.assertIn('HTTP 410', ' '.join(gap['reasons']))
        self.assertIn('HTTP 500', ' '.join(gap['reasons']))

    def test_unmapped_segment_keeps_local_time_only(self):
        segments = [segment(0, 40, None, [CUE(100, 900, '本地时间')])]
        report = build_timeline(segments)
        event = report['timeline'][0]
        self.assertFalse(event['timing']['mapped'])
        self.assertIsNone(event['timing']['start_ticks'])
        self.assertEqual(report['uncertainty']['segments_without_mpegts_map'][0]['sequence'], 40)

    def test_minutes_render_includes_gap_and_evidence(self):
        segments = [
            segment(0, 40, 1000, [CUE(0, 100, '甲')], byterange={'offset': 0, 'length': 9}),
            segment(0, 41, None, [], status='permanent_gap', reason='HTTP 410'),
        ]
        text = render_minutes(build_timeline(segments))
        self.assertIn('缺口声明', text)
        self.assertIn('永久缺口', text)
        self.assertIn('byte', text.lower())
        self.assertIn('证据', text)


if __name__ == '__main__':
    unittest.main()

import tempfile
import threading
import unittest
from pathlib import Path

from captionreach.archive import Archive
from captionreach.collector import Collector, ReachStore, segment_identity
from captionreach.drill_server import DrillServer


class CollectorDrillTests(unittest.TestCase):
    def setUp(self):
        self.server = DrillServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.tempdir = tempfile.mkdtemp(prefix='reach-test-')
        self.db = Path(self.tempdir) / 'c.sqlite3'
        self.objects = Path(self.tempdir) / 'objects'
        self.state_path = Path(self.tempdir) / 'state.json'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def _collector(self):
        archive = Archive(self.db, self.objects)
        store = ReachStore(self.state_path)
        self.addCleanup(archive.close)
        return Collector(self.base + '/live.m3u8', store, archive), store, archive

    def test_full_run_classifications_and_gap(self):
        collector, store, archive = self._collector()
        last = collector.run(max_polls=4)
        self.assertTrue(last['ended'])
        report = collector.report()
        classifications = {event['classification'] for event in report['timeline']}
        self.assertIn('overlap', classifications)
        self.assertIn('conflict', classifications)
        # 永久缺口（序列 44 被源站 410）。
        self.assertTrue(any(gap['kind'] == 'permanent' for gap in report['gaps']))
        gap44 = next(gap for gap in report['gaps'] if gap['from_sequence'] == 44)
        self.assertTrue(gap44['time_uncertain'])

    def test_transient_500_is_retried_within_window(self):
        collector, store, _ = self._collector()
        collector.poll_once()
        collector.poll_once()
        seg42 = next(s for s in store.all_segments() if s['sequence'] == 42)
        # 第一次拉取 500，后续轮询补取成功。
        self.assertEqual(seg42['status'], 'captured')
        self.assertGreaterEqual(self.server.segment_attempts['/segment-42.vtt'], 2)

    def test_gap_visible_before_endlist(self):
        # 关键：缺口信息不能只在 ENDLIST 正常结束时才出现。
        collector, store, _ = self._collector()
        collector.poll_once()
        collector.poll_once()
        early = collector.report()
        self.assertFalse(store.state['ended'])
        self.assertTrue(early['gaps'], '中断前就应能看到缺口声明')
        self.assertTrue(any('410' in reason for gap in early['gaps']
                            for reason in gap['reasons']))

    def test_restart_keeps_timeline_and_does_not_refetch(self):
        collector, store, archive = self._collector()
        collector.poll_once()
        collector.poll_once()
        before = {(s['epoch'], s['sequence']): s['status'] for s in store.all_segments()}
        captures_before = len(archive.rows())
        archive.close()

        archive2 = Archive(self.db, self.objects)
        store2 = ReachStore(self.state_path)
        collector2 = Collector(self.base + '/live.m3u8', store2, archive2)
        result = collector2.poll_once()
        after = {(s['epoch'], s['sequence']): s['status'] for s in store2.all_segments()}
        for key, status in before.items():
            self.assertEqual(after.get(key), status, '重启后已确认时间轴不得归零')
        # 重启轮询只能抓取新出现的分片，不能重抓已确认分片。
        for identity in result['fetched']:
            self.assertNotIn('seq40', identity)
            self.assertNotIn('seq41', identity)
            self.assertNotIn('seq42', identity)
        self.assertGreaterEqual(len(archive2.rows()), captures_before)
        archive2.close()

    def test_segment_identity_requires_range_not_filename(self):
        entry_a = {'sequence': 40, 'uri': 'joined.vtt',
                   'byterange': {'offset': 0, 'length': 100}}
        entry_b = {'sequence': 40, 'uri': 'joined.vtt',
                   'byterange': {'offset': 100, 'length': 50}}
        self.assertNotEqual(segment_identity(entry_a, 0), segment_identity(entry_b, 0))

    def test_ends_only_after_endlist_and_no_pending(self):
        collector, store, _ = self._collector()
        collector.poll_once()
        self.assertFalse(collector._finished())
        collector.run(max_polls=4)
        self.assertTrue(store.state['ended'])
        self.assertTrue(collector._finished())


if __name__ == '__main__':
    unittest.main()

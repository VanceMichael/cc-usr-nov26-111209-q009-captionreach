import unittest

from captionreach.parser import (
    inspect_document, parse_clock, parse_playlist, parse_timestamp_map, parse_webvtt,
)


class ClockTests(unittest.TestCase):
    def test_clock_variants(self):
        self.assertEqual(parse_clock('00:01:02.500'), 62500)
        self.assertEqual(parse_clock('1:02:03.000'), 3723000)
        self.assertEqual(parse_clock('00:00.000'), 0)

    def test_timestamp_map_defaults(self):
        mapping = parse_timestamp_map('LOCAL:00:00:01.000,MPEGTS:90000')
        self.assertEqual(mapping['local_ms'], 1000)
        self.assertEqual(mapping['mpegts'], 90000)
        missing = parse_timestamp_map('LOCAL:00:00:00.000')
        self.assertIsNone(missing['mpegts'])


class PlaylistTests(unittest.TestCase):
    def test_rolling_sequence_is_explicit(self):
        playlist = parse_playlist(
            '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:42\n'
            '#EXTINF:1.5,\na.vtt\n#EXTINF:1.5,\nb.vtt\n')
        self.assertEqual([entry['sequence'] for entry in playlist['entries']], [42, 43])

    def test_byterange_explicit_and_continuation(self):
        playlist = parse_playlist(
            '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'
            '#EXTINF:1,\n#EXT-X-BYTERANGE:100@0\njoined.vtt\n'
            '#EXTINF:1,\n#EXT-X-BYTERANGE:50\njoined.vtt\n')
        first, second = playlist['entries']
        self.assertEqual(first['byterange'], {'length': 100, 'offset': 0})
        # 缺省起点沿用同一 URI 上一分片结束位置。
        self.assertEqual(second['byterange'], {'length': 50, 'offset': 100})

    def test_byterange_continuation_requires_same_uri(self):
        with self.assertRaises(ValueError):
            parse_playlist(
                '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'
                '#EXTINF:1,\n#EXT-X-BYTERANGE:50\nother.vtt\n')

    def test_discontinuity_flags_next_entry(self):
        playlist = parse_playlist(
            '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-DISCONTINUITY-SEQUENCE:2\n'
            '#EXTINF:1,\na.vtt\n#EXT-X-DISCONTINUITY\n#EXTINF:1,\nb.vtt\n')
        self.assertFalse(playlist['entries'][0]['discontinuity'])
        self.assertTrue(playlist['entries'][1]['discontinuity'])
        self.assertEqual(playlist['discontinuity_sequence'], 2)

    def test_encryption_tag_is_fatal_unsupported(self):
        playlist = parse_playlist(
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:1,\na.vtt\n')
        self.assertTrue(playlist['unsupported'][0]['fatal'])
        self.assertIn('EXT-X-KEY', playlist['unsupported'][0]['reason'])

    def test_unknown_ext_tag_reported_not_fatal(self):
        playlist = parse_playlist(
            '#EXTM3U\n#EXT-X-IMAGES-ONLY\n#EXTINF:1,\na.vtt\n')
        self.assertFalse(playlist['unsupported'][0]['fatal'])
        self.assertEqual(playlist['unsupported'][0]['tag'], '#EXT-X-IMAGES-ONLY')

    def test_endlist_and_missing_uri(self):
        ended = parse_playlist('#EXTM3U\n#EXTINF:2,\na.vtt\n#EXT-X-ENDLIST\n')
        self.assertTrue(ended['ended'])
        with self.assertRaises(ValueError):
            inspect_document(b'#EXTM3U\n#EXTINF:2,\n')


class WebVttTests(unittest.TestCase):
    def test_concatenated_documents_share_one_object(self):
        body = (
            'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:100\n\n'
            '00:00:00.500 --> 00:00:01.000\n甲\n\n'
            'WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:200\n\n'
            '00:00:00.200 --> 00:00:00.800\n乙\n'
        ).encode()
        parsed = parse_webvtt(body.decode())
        self.assertEqual(len(parsed['documents']), 2)
        self.assertEqual([cue['document'] for cue in parsed['cues']], [0, 1])
        self.assertEqual(parsed['documents'][1]['map']['mpegts'], 200)

    def test_cue_keeps_local_milliseconds(self):
        parsed = inspect_document(
            ('WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n'
             '00:00:01.000 --> 00:00:02.000\n谢谢').encode())
        cue = parsed['cues'][0]
        self.assertEqual(cue['start_ms'], 1000)
        self.assertEqual(cue['end_ms'], 2000)
        self.assertEqual(cue['text'], '谢谢')

    def test_reject_non_playlist_non_vtt(self):
        with self.assertRaises(ValueError):
            inspect_document(b'hello world')


if __name__ == '__main__':
    unittest.main()

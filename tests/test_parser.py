import unittest
from pathlib import Path
from captionreach.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_playlist_sequence(self):
        result = inspect_document(Path('captionreach/fixtures/live.m3u8').read_bytes())
        self.assertEqual([row['sequence'] for row in result['entries']], [40, 41])
        self.assertFalse(result['ended'])

    def test_vtt_content(self):
        result = inspect_document(Path('captionreach/fixtures/segment-a.vtt').read_bytes())
        self.assertEqual(result['cues'][1]['text'], '谢谢')

    def test_endlist(self):
        self.assertTrue(inspect_document(Path('captionreach/fixtures/slid.m3u8').read_bytes())['ended'])

    def test_missing_uri(self):
        with self.assertRaises(ValueError):
            inspect_document(b'#EXTM3U\n#EXTINF:2,\n')

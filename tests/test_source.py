import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from captionreach.source_server import FixtureServer
from captionreach.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_range_bytes_and_status(self):
        original = self.get('/joined.vtt')[2]
        request = urllib.request.Request(self.base + '/joined.vtt', headers={'Range': 'bytes=4-19'})
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), original[4:20])
            self.assertTrue(response.headers['Content-Range'].startswith('bytes 4-19/'))

    def test_window_slide(self):
        self.phase('slid')
        body = self.get('/live.m3u8')[2]
        self.assertIn(b'#EXT-X-ENDLIST', body)
        self.assertIn(b'#EXT-X-BYTERANGE', body)
        self.assertEqual(self.get('/segment-a.vtt')[0], 410)

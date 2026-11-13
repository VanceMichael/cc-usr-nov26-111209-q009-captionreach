import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from captionreach.archive import Archive
from captionreach.collector import Collector, ReachStore


class _EncryptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def do_GET(self):
        body = (b'#EXTM3U\n#EXT-X-VERSION:3\n'
                b'#EXT-X-KEY:METHOD=AES-128,URI="http://127.0.0.1/key"\n'
                b'#EXTINF:2,\nseg.vtt\n')
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UnsupportedSourceTests(unittest.TestCase):
    def test_encrypted_playlist_is_rejected_with_reason(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _EncryptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/live.m3u8'
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory) / 'c.sqlite3', Path(directory) / 'objects')
            store = ReachStore(Path(directory) / 'state.json')
            collector = Collector(url, store, archive)
            try:
                with self.assertRaises(ValueError) as context:
                    collector.poll_once()
                self.assertIn('EXT-X-KEY', str(context.exception))
                self.assertTrue(store.state['unsupported'])
                self.assertIn('不支持', store.state['unsupported'][0]['reason'])
            finally:
                archive.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()


if __name__ == '__main__':
    unittest.main()

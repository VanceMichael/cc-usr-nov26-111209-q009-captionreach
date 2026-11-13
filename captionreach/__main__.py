DESCRIPTION = '活动字幕 HLS 清单和 WebVTT 分片的查看、抓取、持续采集与原文保存。'
DEMO_PATH = '/live.m3u8'

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from .archive import Archive
from .collector import Collector, ReachStore
from .parser import inspect_document
from .source_server import FixtureServer
from .transport import fetch


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def run_reach(arguments):
    archive = Archive(arguments.db, arguments.objects)
    store = ReachStore(arguments.state)
    collector = Collector(arguments.url, store, archive, sleep=time.sleep)
    try:
        summary = collector.run(max_polls=arguments.max_polls,
                                interval_seconds=arguments.interval)
        report = collector.report()
        if arguments.minutes:
            print(collector.minutes())
        else:
            _print({'last_poll': summary, 'report': report})
    finally:
        archive.close()


def run_demo():
    with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_address[1]) + DEMO_PATH
            status, content_type, body = fetch(url)
            archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
            try:
                capture_id = archive.save(url, status, content_type, body)
                result = {'capture_id': capture_id, 'http_status': status,
                          'observation': inspect_document(archive.read(capture_id))}
            finally:
                archive.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    capture = commands.add_parser('capture')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')
    show = commands.add_parser('show')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')
    reach = commands.add_parser('reach', help='持续采集滚动清单直到 ENDLIST 或达到轮询上限')
    reach.add_argument('url')
    reach.add_argument('--db', default='captures.sqlite3')
    reach.add_argument('--objects', default='objects')
    reach.add_argument('--state', default='reach-state.json')
    reach.add_argument('--max-polls', type=int, default=None)
    reach.add_argument('--interval', type=float, default=1.0)
    reach.add_argument('--minutes', action='store_true', help='打印中文文字纪要而非 JSON')
    commands.add_parser('demo')
    commands.add_parser('drill', help='本地滚动/回绕/缺口演练，打印中断前后纪要')
    arguments = parser.parse_args()
    if arguments.command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
        _print(result)
    elif arguments.command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body),
                          'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
        _print(result)
    elif arguments.command == 'reach':
        run_reach(arguments)
    elif arguments.command == 'drill':
        from .drill import main as drill_main
        drill_main()
    else:
        _print(run_demo())


if __name__ == '__main__':
    main()

"""本地演练编排：跑完整场滚动字幕流并在中断前后打印纪要。

流程：
1. 启动 DrillServer，用 Collector 轮询前两代清单，打印“中断前纪要”
   （此时必须已经能看到永久缺口、临时缺口和时间不确定性）；
2. 关闭采集器并从同一状态文件重新打开，验证重启不归零、不重抓；
3. 轮询到 ENDLIST，打印“恢复后最终纪要”。
全程只取文字分片，不下载音视频。
"""

import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .collector import Collector, ReachStore
from .drill_server import DrillServer


def run_drill(directory=None):
    owned = directory is None
    if owned:
        directory = tempfile.mkdtemp(prefix='captionreach-drill-')
    directory = Path(directory)
    db_path = directory / 'captures.sqlite3'
    objects_path = directory / 'objects'
    state_path = directory / 'reach-state.json'

    server = DrillServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    playlist_url = base + '/live.m3u8'
    output = {'directory': str(directory), 'url': playlist_url, 'stages': []}
    try:
        # ---- 第一阶段：中断前，只轮询两次 ----
        archive = Archive(db_path, objects_path)
        store = ReachStore(state_path)
        collector = Collector(playlist_url, store, archive)
        first = collector.poll_once()
        second = collector.poll_once()
        before = collector.report()
        output['stages'].append({
            'name': '中断前（前两轮清单）',
            'polls': [first, second],
            'report': before,
        })
        capture_count_at_interrupt = len(archive.rows())
        archive.close()

        # ---- 模拟进程重启：全部对象重新从状态文件打开 ----
        archive = Archive(db_path, objects_path)
        reopened_store = ReachStore(state_path)
        restarted = Collector(playlist_url, reopened_store, archive)
        restart_check = restarted.poll_once()
        # 已确认分片不得重新抓取：本轮 fetched 只应包含新窗口里的新分片。
        output['stages'].append({
            'name': '重启后第一轮',
            'poll': restart_check,
            'captures_at_interrupt': capture_count_at_interrupt,
            'captures_after_restart_poll': len(archive.rows()),
        })
        final_poll = restarted.run(max_polls=4)
        after = restarted.report()
        output['stages'].append({'name': 'ENDLIST 后最终', 'poll': final_poll, 'report': after})
        output['minutes_before'] = render_safe(before)
        output['minutes_after'] = restarted.minutes()
        archive.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    return output


def render_safe(report):
    from .timeline import render_minutes
    return render_minutes(report)


def main():
    output = run_drill()
    print('################ 中断前纪要（信息不依赖正常结束） ################')
    print(output['minutes_before'])
    print()
    print('################ 重启后最终纪要 ################')
    print(output['minutes_after'])
    print()
    print('结构化结果写入演练目录: ' + output['directory'])
    Path(output['directory'], 'drill-result.json').write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

import subprocess
import sys
import tempfile
import unittest

from captionreach.drill import run_drill


class DrillEndToEndTests(unittest.TestCase):
    def test_drill_reports_before_and_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            output = run_drill(directory)
            before_text = output['minutes_before']
            after_text = output['minutes_after']
            # 中断前纪要已经包含缺口、回绕和证据，不依赖 ENDLIST。
            self.assertIn('缺口声明', before_text)
            self.assertIn('MPEGTS 回绕', before_text)
            self.assertIn('证据', before_text)
            # 最终纪要包含三分类关键标记与无映射提示。
            self.assertIn('同刻内容冲突', after_text)
            self.assertIn('重叠副本已合并来源', after_text)
            self.assertIn('本地时间(无全局映射)', after_text)
            # 两次“谢谢”都保留，没有被全局文本去重。
            self.assertGreaterEqual(after_text.count('谢谢'), 2)
            # 重启轮询不应重抓已确认分片。
            restart_stage = output['stages'][1]
            for identity in restart_stage['poll']['fetched']:
                self.assertNotIn('seq40', identity)
                self.assertNotIn('seq41', identity)
            self.assertIn('report', output['stages'][-1])

    def test_cli_drill_command(self):
        result = subprocess.run(
            [sys.executable, '-m', 'captionreach', 'drill'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('中断前纪要', result.stdout)
        self.assertIn('重启后最终纪要', result.stdout)


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import brief_digest
from realtime_fallback import PlaybackQueue
from test_state_manager import state_at

SUMMARY = """================================================================================
VIDEO SUMMARY
================================================================================

Title: 반도체 사이클 점검
Channel: 소수몽키
Published: 2026-11-19 07:00
URL: https://www.youtube.com/watch?v=abcdefghijk

--------------------------------------------------------------------------------
OVERVIEW
--------------------------------------------------------------------------------
메모리 가격과 **HBM** 수요를 점검한다.

---

--------------------------------------------------------------------------------
KEY POINTS
--------------------------------------------------------------------------------
- **DRAM 계약가**: 4분기 상승
- **HBM4**: 내년 공급 부족
- 장비 발주 증가
- 네 번째 항목

--------------------------------------------------------------------------------
NOTABLE QUOTES
--------------------------------------------------------------------------------
> "인용"

--------------------------------------------------------------------------------
KEY TAKEAWAYS
--------------------------------------------------------------------------------
공급 부족이 이어진다는 주장.

================================================================================
Generated: 2026-11-19 08:10:00
"""


class FakeSummarizer:
    model = 'm'

    def __init__(self, text='공통 주제: 메모리', error=None):
        def create(**kwargs):
            self.prompt = kwargs['messages'][0]['content']
            if error:
                raise error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class BriefDigestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = PlaybackQueue(self.root / 'realtime.sqlite3')
        self.state = state_at(self.root)

    def summary(self, vid):
        path = self.root / f'{vid}.txt'
        path.write_text(SUMMARY.replace('abcdefghijk', vid), encoding='utf-8')
        self.queue.enqueue({'video_id': vid, 'published_at': '2026-11-19T00:00:00Z'}, 'user_selected', 'daily')
        self.queue.delivered(vid, 'daily', path)
        return path

    def test_parse_keeps_three_points_without_bold_marks(self):
        parsed = brief_digest.parse_summary(SUMMARY)
        self.assertEqual(parsed['title'], '반도체 사이클 점검')
        self.assertEqual(parsed['channel'], '소수몽키')
        self.assertEqual(parsed['overview'], '메모리 가격과 HBM 수요를 점검한다.')
        self.assertEqual(parsed['points'], ['DRAM 계약가: 4분기 상승', 'HBM4: 내년 공급 부족', '장비 발주 증가'])
        self.assertEqual(parsed['takeaways'], '공급 부족이 이어진다는 주장.')

    def test_one_video_gets_no_synthesis_and_a_missing_file_is_reported(self):
        self.summary('video000001')
        fake = FakeSummarizer()
        result = brief_digest.build(['video000001', 'video000009'], fake, self.queue, self.state)
        self.assertEqual([v['video_id'] for v in result['videos']], ['video000001'])
        self.assertEqual(result['missing'], ['video000009'])
        self.assertIsNone(result['synthesis'])
        self.assertFalse(hasattr(fake, 'prompt'))

    def test_two_videos_get_one_synthesis_and_a_model_error_leaves_the_videos(self):
        self.summary('video000001')
        self.summary('video000002')
        fake = FakeSummarizer('공통 주제: **메모리**')
        result = brief_digest.build(['video000001', 'video000002'], fake, self.queue, self.state)
        self.assertEqual(result['synthesis'], '공통 주제: 메모리')
        self.assertIn('[2] 소수몽키 · 반도체 사이클 점검', fake.prompt)
        failed = brief_digest.build(['video000001', 'video000002'], FakeSummarizer(error=RuntimeError('429')),
                                    self.queue, self.state)
        self.assertIsNone(failed['synthesis'])
        self.assertEqual(len(failed['videos']), 2)


if __name__ == '__main__':
    unittest.main()

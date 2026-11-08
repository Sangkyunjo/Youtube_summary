"""Read the official transcript panel's rendered text through a browser adapter.

No network interception, internal player state, cookies or caption URLs are used.
The controller may also import a capture from a regular Safari/Chrome session
through realtime_worker.py import-ui when this UI adapter cannot read the page.
"""
import math
import re
import time
from urllib.parse import parse_qs, urlparse

from transcript_acquisition import UI_SOURCE, validate_ui_document


def parse_segment(text):
    match = re.match(r'^\s*(\d{1,3}:\d{2}(?::\d{2})?)\s+(.+)$', text, re.S)
    if not match:
        return None
    parts = [int(part) for part in match[1].split(':')]
    if any(part >= 60 for part in parts[1:]):
        return None
    start = sum(value * (60 ** i) for i, value in enumerate(reversed(parts)))
    return {'start_seconds': start, 'text': match[2].strip()}


class VisibleTranscriptReader:
    def __init__(self, timeout_seconds=20):
        self.timeout_seconds = timeout_seconds

    def read(self, page, item, cancelled=lambda: False):
        vid, duration = item['video_id'], item.get('duration_seconds')
        if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                or not 1 <= duration <= 21600):
            return None, 'ui_duration_unknown'
        parsed = urlparse(page.url)
        if (parsed.hostname not in ('www.youtube.com', 'youtube.com')
                or parse_qs(parsed.query).get('v') != [vid]):
            return None, 'ui_wrong_video'
        deadline = time.monotonic() + self.timeout_seconds
        # The adapter first inspects the rendered page, then acts on exposed controls.
        metadata = page.locator('ytd-watch-metadata')
        show = page.get_by_role('button', name=re.compile(r'^(스크립트 표시|Show transcript)$', re.I))
        expanded = False
        while time.monotonic() < deadline:
            if cancelled():
                return None, 'stopped'
            if show.count() and show.first.is_visible():
                show.first.click(timeout=2000)
                break
            if not expanded:
                more = metadata.get_by_role('button', name=re.compile(r'^(더보기|Show more|…more|\.\.\.more)$', re.I))
                if more.count() and more.first.is_visible():
                    more.first.click(timeout=2000)
                    expanded = True
            page.wait_for_timeout(250)
        else:
            return None, 'ui_transcript_not_offered'

        collected, stable, previous = {}, 0, None
        while time.monotonic() < deadline:
            if cancelled():
                return None, 'stopped'
            if parse_qs(urlparse(page.url).query).get('v') != [vid]:
                return None, 'ui_wrong_video'
            panels = page.locator('ytd-transcript-renderer, transcript-view-model')
            panel = next((p for p in panels.all() if p.is_visible()), None)
            if panel is None:
                page.wait_for_timeout(250)
                continue
            rows = panel.locator('ytd-transcript-segment-renderer, [role="button"]')
            last = None
            for row in rows.all():
                if time.monotonic() >= deadline:
                    return None, 'ui_incomplete_or_loading'
                if not row.is_visible():
                    continue
                segment = parse_segment(row.inner_text(timeout=1000))
                if segment:
                    collected[(segment['start_seconds'], segment['text'])] = segment
                    last = row
            if last is not None:
                last.scroll_into_view_if_needed(timeout=1500)
            signature = tuple(collected)
            stable = stable + 1 if signature == previous else 0
            previous = signature
            ordered = sorted(collected.values(), key=lambda s: s['start_seconds'])
            tail = min(45, max(15, duration * .15))
            near_end = bool(ordered) and duration - tail <= ordered[-1]['start_seconds'] <= duration
            if stable >= 2 and near_end:
                document = dict(video_id=vid, source=UI_SOURCE, captured_at=time.time(),
                                complete=True, language=None, generated=None, segments=ordered)
                try:
                    validate_ui_document(vid, document)
                except ValueError:
                    return None, 'ui_transcript_unusable'
                return document, 'ok'
            page.wait_for_timeout(250)
        return None, 'ui_incomplete_or_loading'

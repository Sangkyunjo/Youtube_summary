"""Video part of the morning Telegram briefing: short per-video summaries and one synthesis.

User request 2026-11-19: the briefing carries the videos the user picked that morning, each in a
few lines, plus a synthesis across them (shared topics, disagreements, names mentioned). The
per-video lines are cut from the daily summary files already written; the synthesis is one more
call of the configured summary model, made only when two or more videos are summarized. It is a
reading aid: source-grounded, no advice, and its failure leaves the per-video lines.

Runs in the Airflow container (``main.py --brief-digest IDS``), where the model key lives.
"""

import json
import re
from pathlib import Path

from realtime_fallback import PlaybackQueue
from state_manager import StateManager
from ytsum_utils import get_summaries_dir, setup_logging

logger = setup_logging("brief_digest")

SECTION = re.compile(r'^-{20,}\n([A-Z ]+)\n-{20,}\n(.*?)(?=^-{20,}\n[A-Z ]+\n-{20,}\n|^={20,}|\Z)', re.M | re.S)
HEADER = re.compile(r'^(Title|Channel|Published|URL):\s*(.*)$', re.M)
OVERVIEW_CHARS = 260
POINT_CHARS = 140
POINTS = 3

SYNTHESIS_PROMPT = """아래는 오늘 아침 사용자가 고른 유튜브 영상들의 요약이다. 영상들을 가로질러 정리하라.

규칙:
- 요약에 있는 내용만 쓴다. 새 사실·전망·매매 조언을 덧붙이지 않는다.
- 누가 말했는지(채널)를 붙인다. 같은 주제에 견해가 갈리면 갈린 그대로 적는다.
- 한국어 평문, 900자 이내. 마크다운 굵게(**)와 표를 쓰지 않는다.

형식:
공통 주제: 한두 줄
엇갈린 견해: 한두 줄(없으면 "없음")
언급된 종목·자산: 쉼표로 나열(없으면 "없음")
확인해 볼 것: 요약에서 사실 확인이 필요해 보이는 주장 한두 개

영상 요약:
{videos}"""


def _clean(text: str) -> str:
    return re.sub(r'[ \t]+', ' ', text.replace('**', '')).strip()


def _cut(text: str, limit: int) -> str:
    text = _clean(text).replace('\n', ' ')
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def parse_summary(text: str) -> dict:
    """Header fields and sections of a daily summary file (config.yaml summary_template)."""
    fields = {key.lower(): value.strip() for key, value in HEADER.findall(text)}
    sections = {name.strip(): body.strip().strip('-').strip() for name, body in SECTION.findall(text)}
    points = [line.lstrip('-•* ').strip() for line in sections.get('KEY POINTS', '').splitlines()
              if line.strip().startswith(('-', '•', '*'))]
    return {"title": fields.get("title"), "channel": fields.get("channel"), "url": fields.get("url"),
            "published": fields.get("published"), "overview": _cut(sections.get('OVERVIEW', ''), OVERVIEW_CHARS),
            "points": [_cut(point, POINT_CHARS) for point in points[:POINTS] if point],
            "takeaways": _clean(sections.get('KEY TAKEAWAYS', ''))}


def summary_path(vid: str, queue=None, state=None) -> Path | None:
    """The daily summary file of a video: its delivery record, else the state record, else its id suffix."""
    queue = queue or PlaybackQueue()
    state = state or StateManager()
    delivered = queue.delivery_output(vid, 'daily')
    if delivered and Path(delivered).is_file():
        return Path(delivered)
    root = get_summaries_dir()
    record = state.get_video_metadata(vid, 'daily')
    if record and record.get('summary_file'):
        found = next(root.rglob(record['summary_file']), None)
        if found:
            return found
    return next(root.rglob(f"*_{vid.encode('utf-8').hex()}.txt"), None)


def synthesize(videos: list[dict], summarizer=None) -> str | None:
    """One model call across the videos; None when it fails or returns nothing."""
    if len(videos) < 2:
        return None
    blocks = []
    for number, video in enumerate(videos, 1):
        points = "\n".join(f"- {p}" for p in video["points"])
        blocks.append(f"[{number}] {video['channel']} · {video['title']}\n개요: {video['overview']}\n"
                      f"핵심:\n{points}\n결론: {_cut(video.get('takeaways') or '', 400)}")
    try:
        if summarizer is None:
            from summarizer import Summarizer
            summarizer = Summarizer()
        response = summarizer.client.chat.completions.create(
            model=summarizer.model, max_tokens=1200,
            messages=[{"role": "user", "content": SYNTHESIS_PROMPT.format(videos="\n\n".join(blocks))}])
        text = response.choices[0].message.content if response.choices else None
    except Exception as exc:  # the per-video lines still go out
        logger.error("synthesis failed: %s", exc)
        return None
    return _clean(text) if text and text.strip() else None


def build(video_ids: list[str], summarizer=None, queue=None, state=None) -> dict:
    videos, missing = [], []
    for vid in video_ids:
        path = summary_path(vid, queue, state)
        if path is None:
            missing.append(vid)
            continue
        video = parse_summary(path.read_text(encoding='utf-8'))
        video.update(video_id=vid, url=video.get("url") or f"https://youtu.be/{vid}")
        videos.append(video)
    return {"videos": videos, "missing": missing, "synthesis": synthesize(videos, summarizer)}


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1].split(',')), ensure_ascii=False))

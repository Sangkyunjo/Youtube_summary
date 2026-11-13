"""Transcript provenance, visible-UI imports and summary review metadata.

This module has no browser, recording or paid API dependencies. Acquisition
workers supply text and evidence; the container only summarizes saved input.
"""
import hashlib
import json
import math
import re
import time
from pathlib import Path

from ytsum_utils import atomic_write_text

API_SOURCE = 'youtube_captions_api'
UI_SOURCE = 'youtube_visible_transcript'
LOCAL_SOURCE = 'local_whisper'


def acquisition(source, *, engine, model=None, language=None, generated=None,
                segments=None, evidence=None):
    return dict(source=source, engine=engine, model=model, language=language,
                generated=generated, segments=segments or [], evidence=evidence or {})


def from_extractor(extractor):
    info = getattr(extractor, 'last_track_info', None)
    info = info if isinstance(info, dict) else {}
    segments = getattr(extractor, 'last_segments', None)
    engine = getattr(extractor, 'engine', 'youtube-transcript-api')
    engine = engine if isinstance(engine, str) else 'youtube-transcript-api'
    model = getattr(extractor, 'model', None)
    return acquisition('direct_media' if engine == 'whisper' else API_SOURCE,
                       engine=engine, model=model if isinstance(model, str) else None,
                       language=info.get('language'), generated=info.get('generated'),
                       segments=segments if isinstance(segments, list) else [])


def from_job(job):
    if isinstance(job.get('acquisition'), dict) and job['acquisition'].get('source'):
        return job['acquisition']
    # Read old databases without rewriting their transcript or guessing API language.
    if job.get('model') == 'youtube-captions':
        return acquisition('youtube_captions_legacy', engine='youtube-captions')
    return acquisition(LOCAL_SOURCE, engine='whisper-cpp', model=job.get('model'),
                       language='ko', generated=True)


def validate_ui_document(vid, document):
    """Accept only explicitly complete, video-bound visible transcript captures.

    `complete` is an assertion by the browser controller that it reached the
    panel's end. Timestamps supplied by the UI are kept; durations are never
    synthesized from the next row or total video duration.
    """
    if document.get('video_id') != vid:
        raise ValueError('UI transcript belongs to a different video')
    if document.get('source') != UI_SOURCE or document.get('complete') is not True:
        raise ValueError('A complete visible YouTube transcript capture is required')
    captured = document.get('captured_at')
    if (isinstance(captured, bool) or not isinstance(captured, (int, float))
            or not math.isfinite(captured) or not 0 < captured <= time.time() + 15):
        raise ValueError('Valid UI capture timestamp required')
    rows = document.get('segments')
    if not isinstance(rows, list) or not rows:
        raise ValueError('UI transcript segments are missing')
    normalized, previous = [], -1.0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid UI transcript segment')
        start = row.get('start_seconds', row.get('start'))
        text = row.get('text')
        if (isinstance(start, bool) or not isinstance(start, (int, float))
                or not math.isfinite(start) or start < previous or start < 0
                or not isinstance(text, str) or not text.strip()):
            raise ValueError('UI segment text and ordered finite timestamps required')
        segment = dict(start=float(start), text=text.strip())
        if 'duration' in row:
            duration = row['duration']
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration < 0):
                raise ValueError('Invalid caption duration')
            segment['duration'] = float(duration)
        normalized.append(segment)
        previous = start
    text = ' '.join(row['text'] for row in normalized)
    if len(text.strip()) < 200:
        raise ValueError('Transcript too short; keeping job retryable')
    language = document.get('language')
    generated = document.get('generated')
    if language is not None and not isinstance(language, str):
        raise ValueError('Invalid caption language')
    if generated is not None and not isinstance(generated, bool):
        raise ValueError('Invalid caption generation flag')
    return text, acquisition(UI_SOURCE, engine='youtube-visible-transcript',
                             language=language, generated=generated, segments=normalized,
                             evidence={'captured_at': captured, 'complete': True})


def review_summary(transcript, sections):
    """Flag automated output for review; this is not an accuracy certification."""
    flags = ['speaker_and_proper_names_unverified']
    quotes = sections.get('quotes', '')
    compact = re.sub(r'\s+', '', transcript)
    if isinstance(quotes, str):
        for quote in re.findall(r'["“]([^"”]+)["”]', quotes):
            if re.sub(r'\s+', '', quote) not in compact:
                flags.append('quote_not_verbatim')
                break
    if len(transcript) > 100000:
        flags.append('input_truncated_by_summarizer')
    return {'status': 'pending', 'required': True, 'flags': flags}


def summary_metadata(vid, transcript, sections, source):
    return {'video_id': vid, 'acquisition': source,
            'transcript_sha256': hashlib.sha256(transcript.encode('utf-8')).hexdigest(),
            'review': review_summary(transcript, sections)}


def write_metadata(path, metadata):
    target = Path(path).with_suffix('.meta.json')
    atomic_write_text(target, json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False))
    return target

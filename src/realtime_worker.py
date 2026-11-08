"""Mac-side supervised playback worker; never downloads YouTube media."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import wave

from realtime_fallback import PlaybackQueue, playback_segments, validate_playback, video_id


def fanding_root():
    return Path(os.environ.get('FKB_ROOT', '~/PersonalAgent/fanding-kb')).expanduser().resolve()


def preflight():
    if sys.platform != 'darwin':
        raise RuntimeError('Run this worker on the Mac host, outside Docker')
    root = fanding_root()
    sys.path.insert(0, str(root / 'lib'))
    from common import load_config
    from transcribe import _find_cpp_model, _which_cpp
    cfg = load_config()
    binary, model = _which_cpp(cfg), _find_cpp_model(cfg)
    if not binary or not model:
        raise RuntimeError('Local whisper.cpp binary or model is missing')
    return cfg, binary, model


def _concat_filter(segments):
    """ffmpeg filter that keeps only the verified content ranges, in order."""
    trims = ''.join(f'[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[s{i}];'
                    for i, (start, end) in enumerate(segments))
    joined = ''.join(f'[s{i}]' for i in range(len(segments)))
    return f'{trims}{joined}concat=n={len(segments)}:v=0:a=1[out]'


def record(queue, vid, seconds, cut_interruptions=False, capture_device=None, output_device=None):
    cfg, binary, model = preflight()
    if not 5 <= seconds <= 21615:
        raise ValueError('Recording duration must be 5..21615 seconds')
    from capture_lock import acquire
    fd = acquire()
    try:
        queue.claim(vid)
    except BaseException:
        os.close(fd)
        os.environ.pop('VIDEO_CAPTURE_LOCK_FD', None)
        raise
    # Profile-free config prevents YouTube audio/transcripts from entering a
    # paid-course workspace. Existing capture checks and output repair are reused.
    cfg = dict(cfg, workspace='', vault='', default_profile=None)
    if capture_device:
        # Automated playback records from its own loopback, leaving the course setup alone.
        cfg['audio'] = dict(cfg['audio'], device_name=capture_device,
                            output_device_name=output_device or cfg['audio'].get('output_device_name'))
    env = os.environ.copy()
    recorder = None
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, interrupted)
    temp_area = tempfile.TemporaryDirectory(prefix='youtube-playback-')
    try:
        temp = temp_area.name
        import yaml
        cfg['workspace'] = temp
        cfg['vault'] = temp
        config_path = Path(temp) / 'config.yaml'
        config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        env['FKB_CONFIG'] = str(config_path)
        env.pop('FKB_PROFILE', None)
        progress = Path(temp) / 'capture-progress.txt'
        env['VIDEO_CAPTURE_PROGRESS_FILE'] = str(progress)
        print(json.dumps({'video_id': vid, 'status': 'starting',
                          'instructions': 'Wait for fkb-rec recording-start, then play from zero at 1x; observe every <=30 seconds.'}), flush=True)
        recorder = subprocess.Popen(
            [str(fanding_root() / 'bin/fkb-rec'), vid, str(seconds)],
            env=env, pass_fds=(fd,))
        capture_started = False
        stop_sent = False
        while recorder.poll() is None:
            current = queue.get(vid)
            samples = current['evidence']
            if samples and not stop_sent:
                last = samples[-1]
                abnormal = (last.get('ad') is not False or last.get('muted') is not False
                            or last.get('buffering') is not False or last.get('rate') != 1
                            or (last.get('paused') and not last.get('ended')))
                # Automated playback keeps recording through ads and stalls; they are cut later.
                if last.get('video_id') != vid or (abnormal and not cut_interruptions):
                    raise ValueError('Playback interrupted; recording rejected')
                if time.time() - last['observed_at'] > 50:
                    raise ValueError('Playback observer stopped; recording rejected')
                # An ad's own end also reports ended; only the content end stops recording.
                if last.get('ended') and last.get('ad') is False:
                    recorder.send_signal(signal.SIGTERM)
                    stop_sent = True
            elif not samples and capture_started and time.time() - current['started'] > 45:
                raise ValueError('No timely playback start observation')
            if not capture_started and progress.exists():
                stamps = [line.split('=', 1)[1] for line in progress.read_text().splitlines()
                          if line.startswith('out_time_us=')]
                if stamps and stamps[-1].isdigit() and int(stamps[-1]) > 0:
                    queue.started(vid, time.time() - int(stamps[-1]) / 1_000_000)
                    capture_started = True
                    print('CAPTURE_READY: play from zero now, then submit player observations.', flush=True)
            time.sleep(0.1)
        rc = recorder.returncode
        if not capture_started:
            raise RuntimeError('Capture produced no timing evidence')
        if rc != 0:
            raise RuntimeError(f'Audio capture failed (exit {rc})')
        audio = Path(temp) / 'audio' / f'{vid}.wav'
        with wave.open(str(audio)) as wav:
            length = wav.getnframes() / wav.getframerate()
        clean = Path(temp) / 'verified.wav'
        if cut_interruptions:
            segments, _ = playback_segments(queue.get(vid), length, time.time())
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(audio),
                            '-filter_complex', _concat_filter(segments), '-map', '[out]',
                            '-ar', '16000', '-ac', '1', str(clean)], check=True)
        else:
            offset, duration = validate_playback(queue.get(vid), length, time.time())
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(audio),
                            '-ss', str(offset), '-t', str(duration), '-ar', '16000',
                            '-ac', '1', str(clean)], check=True)
        out = Path(temp) / 'transcript'
        print('Playback verified; transcribing locally.', flush=True)
        subprocess.run([binary, '-m', model, '-f', str(clean), '-l', 'ko',
                        '-otxt', '-of', str(out), '-t', str(min(os.cpu_count() or 4, 8))],
                       check=True, timeout=3600)
        from transcribe import cleanup
        text = cleanup(out.with_suffix('.txt').read_text(encoding='utf-8', errors='replace'))
        queue.finish(vid, text, Path(model).name)
        print(json.dumps({'video_id': vid, 'status': 'ready', 'characters': len(text)}), flush=True)
    except BaseException as e:
        if recorder is not None and recorder.poll() is None:
            recorder.send_signal(signal.SIGTERM)
            try:
                recorder.wait(timeout=15)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait()
        queue.fail(vid, str(e) or type(e).__name__)
        raise
    finally:
        temp_area.cleanup()
        signal.signal(signal.SIGTERM, old_term)
        os.close(fd)
        os.environ.pop('VIDEO_CAPTURE_LOCK_FD', None)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('check')
    imp = sub.add_parser('import-ui', help='Import a complete transcript read from the official UI')
    imp.add_argument('video_id'); imp.add_argument('--file', type=Path, required=True)
    unavailable = sub.add_parser('ui-unavailable', help='Advance a checked UI job to supervised playback')
    unavailable.add_argument('video_id'); unavailable.add_argument('--reason', required=True)
    retry = sub.add_parser('retry-ui', help='Retry the official transcript panel before recording')
    retry.add_argument('video_id')
    show = sub.add_parser('show'); show.add_argument('video_id')
    rec = sub.add_parser('record'); rec.add_argument('video_id'); rec.add_argument('--seconds', type=int, required=True)
    rec.add_argument('--cut-interruptions', action='store_true',
                     help='Automated playback: cut ads and stalls instead of rejecting the recording')
    rec.add_argument('--capture-device', help='Input device to record instead of the configured one')
    rec.add_argument('--output-device', help='System output that must feed --capture-device')
    obs = sub.add_parser('observe'); obs.add_argument('video_id'); obs.add_argument('--file', type=Path, required=True)
    reset = sub.add_parser('recover'); reset.add_argument('video_id')
    a = p.parse_args()
    q = PlaybackQueue()
    if a.command == 'check':
        cfg, binary, model = preflight()
        print(json.dumps({'binary': binary, 'model': model, 'audio': cfg['audio'],
                          'database': str(q.path)}, ensure_ascii=False, indent=2))
    elif a.command == 'list':
        print(json.dumps(q.list(), ensure_ascii=False, indent=2))
    elif a.command == 'import-ui':
        job = q.import_ui(a.video_id, json.loads(a.file.read_text(encoding='utf-8')))
        print(json.dumps({'video_id': job['video_id'], 'status': job['status'],
                          'source': job['acquisition']['source'],
                          'characters': len(job['transcript']), 'next_action': job['next_action']}))
    elif a.command == 'ui-unavailable':
        q.ui_unavailable(a.video_id, a.reason)
        print(json.dumps({'video_id': a.video_id, 'next_action': 'playback'}))
    elif a.command == 'retry-ui':
        q.retry_ui(a.video_id)
        print(json.dumps({'video_id': a.video_id, 'next_action': 'ui_transcript'}))
    elif a.command == 'show':
        job = q.get(a.video_id)
        if not job:
            raise ValueError('Unknown video')
        job.pop('transcript', None)
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif a.command == 'observe':
        q.observe(a.video_id, json.loads(a.file.read_text()))
    elif a.command == 'recover':
        # Recovery cannot reset a live recorder: take the same host lock first.
        sys.path.insert(0, str(fanding_root() / 'lib'))
        from capture_lock import acquire
        fd = acquire()
        try:
            job = q.get(a.video_id)
            if not job or job['status'] != 'recording':
                raise ValueError('Only interrupted recordings can be recovered')
            q.fail(a.video_id, 'Recovered interrupted capture; retry from start')
        finally:
            os.close(fd)
    else:
        record(q, video_id(a.video_id), a.seconds, a.cut_interruptions,
               a.capture_device, a.output_device)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError) as e:
        sys.exit(str(e))

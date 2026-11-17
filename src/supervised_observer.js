// Supervised playback helpers for the Claude desktop app's browser pane.
// Paste this whole file once into the YouTube watch tab (javascript tool), then:
//   await window.__ytPrepare('VIDEO_ID')  -> waits out pre-roll ads, pauses content at 0
//   await window.__ytStart()              -> plays from 0, returns the first sample list
//   await window.__ytStep(13000)          -> next sample list (JSON) to submit with
//                                            realtime_worker.py observe VIDEO_ID --file
// Read-only except play/pause/seek-to-0 of the visible <video>; it never touches
// stream URLs, player internals or authentication.
(() => {
  const sample = () => {
    const v = document.querySelector('video');
    const p = document.querySelector('#movie_player');
    const errorBox = document.querySelector('.ytp-error');
    const ad = !!(p && p.classList.contains('ad-showing'));
    return {
      video_id: new URL(location.href).searchParams.get('v'),
      current_time: v ? v.currentTime : null, duration: v ? v.duration : null,
      rate: v ? v.playbackRate : null, paused: v ? v.paused : null,
      ended: !!(v && v.ended && !ad), muted: v ? (v.muted || v.volume === 0) : null, ad,
      // Visible ended state; after a post-roll ad the <video> is unloaded and only this remains.
      player_ended: !!(p && p.classList.contains('ended-mode')) && !ad,
      buffering: v ? (!v.ended && (v.readyState < 3 || !isFinite(v.duration))) : true,
      error: errorBox && errorBox.offsetParent !== null ? errorBox.innerText.trim() : null,
      captured_at: Date.now() / 1000,
    };
  };
  const interrupted = (s) => s.ad || s.paused || s.buffering || s.muted || s.rate !== 1;
  const state = (s) => [interrupted(s), s.ad, s.player_ended, s.ended].join();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => { const { error, ...rest } = s; return rest; };

  window.__ytSample = sample;

  // Call repeatedly until ready: one browser tool call is limited to about 45 s.
  window.__ytPrepare = async (vid, maxMs = 35000) => {
    const start = Date.now();
    while (Date.now() - start < maxMs) {
      const s = sample();
      if (s.error) return JSON.stringify({ ready: false, error: s.error });
      const v = document.querySelector('video');
      if (s.video_id === vid && v && !s.ad && isFinite(s.duration) && s.duration > 1) {
        v.pause(); v.currentTime = 0; v.playbackRate = 1; v.muted = false;
        if (v.volume === 0) v.volume = 1;
        await sleep(500);
        return JSON.stringify(Object.assign({ ready: true }, sample()));
      }
      // The transcript panel or a previous pause can leave the ad paused; it never ends then.
      if (s.ad && v && v.paused) v.play();
      await sleep(1000);
    }
    return JSON.stringify({ ready: false, waiting: true });
  };

  window.__ytStart = async () => {
    const v = document.querySelector('video');
    v.currentTime = 0;
    await v.play();
    await sleep(500);
    return JSON.stringify([clean(sample())]);
  };

  // Returns [latest] after maxMs, or [just before, just after] as soon as the
  // content ends, an ad/stall starts or stops, the video changes or an error shows,
  // so cut mode can cut an interruption at its real edges.
  window.__ytStep = async (maxMs = 13000) => {
    let prev = sample();
    const start = Date.now();
    while (Date.now() - start < maxMs) {
      await sleep(500);
      const cur = sample();
      if (cur.error) return JSON.stringify({ error: cur.error, samples: [clean(prev)] });
      if (cur.ended || cur.player_ended || cur.video_id !== prev.video_id || state(cur) !== state(prev)) {
        return JSON.stringify([clean(prev), clean(cur)]);
      }
      prev = cur;
    }
    return JSON.stringify([clean(prev)]);
  };
  return 'ready';
})();

// Supervised playback helpers for the Claude desktop app's browser pane.
// Paste this whole file once into the YouTube watch tab (javascript tool), then:
//   await window.__ytPrepare('VIDEO_ID')  -> waits out pre-roll ads, pauses content at 0
//   await window.__ytStart()              -> plays from 0, returns the first sample list
//   await window.__ytStep(13000)          -> next sample list (JSON) to submit with
//                                            realtime_worker.py observe VIDEO_ID --file
// A sampler inside the page keeps the samples around every ad/stall/end edge, also
// while no tool call is running, so call __ytStep again right after each submission.
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

  // Every 250 ms, keep the samples on both sides of each state change. An ad or a
  // stall that ends between two tool calls is otherwise first seen by the next
  // call, many seconds after the content resumed, and the recording is rejected.
  // Re-pasting the file replaces the sampler.
  if (window.__ytTimer) clearInterval(window.__ytTimer);
  let last = sample(), edges = [], returnedAt = 0, errorSeen = null;
  // The worker accepts only samples newer than the ones it already has.
  const keep = (s) => {
    const newest = edges.length ? edges[edges.length - 1].captured_at : returnedAt;
    if (s.captured_at > newest) edges.push(s);
  };
  // The sampler and __ytStep's own last read both pass through here, so whichever
  // sees a change first still keeps the sample just before it.
  const track = (cur) => {
    if (cur.error && !errorSeen) errorSeen = cur.error;
    if (cur.video_id !== last.video_id || state(cur) !== state(last)) {
      keep(last); keep(cur);
      if (edges.length > 400) edges.splice(0, edges.length - 400);
    }
    last = cur;
  };
  window.__ytTimer = setInterval(() => track(sample()), 250);

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
    const first = sample();
    // Edges from preparation (pre-roll ads, the pause at 0) precede the recording.
    edges = []; errorSeen = null; last = first; returnedAt = first.captured_at;
    return JSON.stringify([clean(first)]);
  };

  // Returns the edge samples kept since the previous call followed by the latest
  // sample. It returns at once when an edge is already kept (the content ended, an
  // ad/stall started or stopped, or the video changed, even between calls) and
  // otherwise as soon as one occurs or after maxMs, so cut mode cuts each
  // interruption at its real edges.
  window.__ytStep = async (maxMs = 13000) => {
    const start = Date.now();
    while (!edges.length && !errorSeen && !last.ended && !last.player_ended
           && Date.now() - start < maxMs) {
      await sleep(250);
    }
    const cur = sample();
    track(cur);
    keep(cur);
    const out = edges.map(clean);
    edges = [];
    if (out.length) returnedAt = out[out.length - 1].captured_at;
    if (errorSeen) return JSON.stringify({ error: errorSeen, samples: out });
    return JSON.stringify(out);
  };
  return 'ready';
})();

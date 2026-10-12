import { api, csrfToken, deviceId, fmt } from './core.js';

export function registerPlayer(Alpine) {
  Alpine.store('player', {
    session: null,
    item: null,
    tracks: [],
    chapters: [],
    playing: false,
    loading: false,
    time: 0,
    duration: 0,
    rate: parseFloat(localStorage.getItem('abstream_rate') || '1'),
    trackIdx: -1,
    listened: 0,
    lastTick: 0,
    syncTimer: null,
    showChapters: false,
    sleepAt: null,

    get audio() { return document.getElementById('audio-el'); },
    get active() { return !!this.session; },
    get chapter() {
      return this.chapters.find(c => this.time >= c.start && this.time < c.end) || null;
    },
    get chapterIndex() { return this.chapters.indexOf(this.chapter); },

    async play(itemId, startAt = null) {
      if (this.session && this.session.libraryItemId === itemId) {
        if (startAt !== null) this.seek(startAt);
        this.resume();
        return;
      }
      await this.stop();
      this.loading = true;
      try {
        const s = await api('POST', `/api/items/${itemId}/play`, {
          deviceInfo: { clientName: 'Audivault Web', deviceId: deviceId(), browserName: navigator.userAgent.split(' ').pop() },
          mediaPlayer: 'html5', forceDirectPlay: true, supportedMimeTypes: ['audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/flac', 'audio/aac', 'audio/wav', 'audio/webm'],
        });
        this.session = s;
        this.item = s.libraryItem;
        this.tracks = s.audioTracks || [];
        this.chapters = s.chapters || [];
        this.duration = s.duration;
        this.listened = 0;
        document.documentElement.style.setProperty('--player-h', '120px');
        this.bindAudio();
        this.loadAt(startAt !== null ? startAt : (s.currentTime || 0), true);
        this.syncTimer = setInterval(() => this.sync(), 15000);
        this.setupMediaSession();
      } catch (e) {
        Alpine.store('app').toast(e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    bindAudio() {
      const a = this.audio;
      if (a._bound) return;
      a._bound = true;
      a.addEventListener('timeupdate', () => {
        const t = this.tracks[this.trackIdx];
        if (!t) return;
        this.time = t.startOffset + a.currentTime;
        const now = performance.now();
        if (this.playing && this.lastTick) this.listened += Math.min(5, (now - this.lastTick) / 1000);
        this.lastTick = now;
        if (this.sleepAt && Date.now() >= this.sleepAt) { this.sleepAt = null; this.pause(); }
        if (this.sleepAt === 'chapter' && this.chapter && this.time >= this.chapter.end - 0.6) { this.sleepAt = null; this.pause(); }
      });
      a.addEventListener('play', () => { this.playing = true; this.lastTick = performance.now(); });
      a.addEventListener('pause', () => { this.playing = false; this.lastTick = 0; });
      a.addEventListener('ended', () => {
        if (this.trackIdx < this.tracks.length - 1) {
          this.loadTrack(this.trackIdx + 1, 0, true);
        } else {
          this.playing = false;
          this.sync();
        }
      });
      a.addEventListener('error', () => {
        if (this.session) Alpine.store('app').toast('Playback error: this browser may not support the audio format.', 'error');
      });
    },

    trackFor(time) {
      for (let i = 0; i < this.tracks.length; i++) {
        const t = this.tracks[i];
        if (time < t.startOffset + t.duration || i === this.tracks.length - 1) return i;
      }
      return 0;
    },

    loadTrack(idx, offset, autoplay) {
      const a = this.audio;
      const t = this.tracks[idx];
      if (!t) return;
      if (this.trackIdx !== idx || !a.src) {
        this.trackIdx = idx;
        a.src = `/public/session/${this.session.id}/track/${t.index}`;
        a.playbackRate = this.rate;
        const start = () => {
          a.currentTime = Math.max(0, offset);
          a.playbackRate = this.rate;
          if (autoplay) a.play().catch(() => { });
        };
        a.addEventListener('loadedmetadata', start, { once: true });
        a.load();
      } else {
        a.currentTime = Math.max(0, offset);
        if (autoplay) a.play().catch(() => { });
      }
      this.time = t.startOffset + offset;
    },

    loadAt(time, autoplay) {
      time = Math.min(Math.max(0, time), Math.max(0, this.duration - 0.5));
      const idx = this.trackFor(time);
      this.loadTrack(idx, time - this.tracks[idx].startOffset, autoplay);
    },

    seek(time) { this.loadAt(time, this.playing); },
    jump(delta) { this.seek(this.time + delta); },
    toggle() { this.playing ? this.pause() : this.resume(); },
    resume() { this.audio.play().catch(() => { }); },
    pause() { this.audio.pause(); this.sync(); },
    prevChapter() {
      const i = this.chapterIndex;
      if (i < 0) return this.seek(0);
      const c = this.chapters[i];
      if (this.time - c.start > 3 || i === 0) this.seek(c.start);
      else this.seek(this.chapters[i - 1].start);
    },
    nextChapter() {
      const i = this.chapterIndex;
      if (i >= 0 && i < this.chapters.length - 1) this.seek(this.chapters[i + 1].start);
    },
    setRate(r) {
      this.rate = r;
      localStorage.setItem('abstream_rate', String(r));
      if (this.audio) this.audio.playbackRate = r;
    },
    setSleep(minutes) {
      if (minutes === 'chapter') this.sleepAt = 'chapter';
      else this.sleepAt = minutes ? Date.now() + minutes * 60000 : null;
    },
    get sleepLabel() {
      if (!this.sleepAt) return '';
      if (this.sleepAt === 'chapter') return 'End of chapter';
      return fmt.clock((this.sleepAt - Date.now()) / 1000);
    },

    async sync() {
      if (!this.session) return;
      const listened = this.listened;
      this.listened = 0;
      try {
        await api('POST', `/api/session/${this.session.id}/sync`, { currentTime: this.time, timeListened: listened, duration: this.duration });
        const app = Alpine.store('app');
        const existing = app.progressMap[this.session.libraryItemId] || {};
        app.progressMap = { ...app.progressMap, [this.session.libraryItemId]: { ...existing, currentTime: this.time, progress: this.duration ? this.time / this.duration : 0, libraryItemId: this.session.libraryItemId } };
      } catch (e) { this.listened += listened; }
    },

    async stop() {
      if (!this.session) return;
      clearInterval(this.syncTimer);
      const s = this.session;
      const listened = this.listened;
      this.audio.pause();
      this.audio.removeAttribute('src');
      this.audio.load();
      this.session = null;
      this.item = null;
      this.trackIdx = -1;
      this.playing = false;
      document.documentElement.style.setProperty('--player-h', '0px');
      try { await api('POST', `/api/session/${s.id}/close`, { currentTime: this.time, timeListened: listened, duration: this.duration }); } catch (e) { }
      try { await Alpine.store('app').refreshUser(); } catch (e) { }
    },

    setupMediaSession() {
      if (!('mediaSession' in navigator) || !this.item) return;
      const md = this.item.media.metadata;
      navigator.mediaSession.metadata = new MediaMetadata({
        title: md.title, artist: md.authorName, album: md.seriesName || md.title,
        artwork: this.item.media.coverPath ? [{ src: `/api/items/${this.item.id}/cover`, sizes: '512x512' }] : [],
      });
      navigator.mediaSession.setActionHandler('play', () => this.resume());
      navigator.mediaSession.setActionHandler('pause', () => this.pause());
      navigator.mediaSession.setActionHandler('seekbackward', () => this.jump(-10));
      navigator.mediaSession.setActionHandler('seekforward', () => this.jump(30));
      navigator.mediaSession.setActionHandler('previoustrack', () => this.prevChapter());
      navigator.mediaSession.setActionHandler('nexttrack', () => this.nextChapter());
    },
  });

}

export function bindPlayerKeys() {
window.addEventListener('beforeunload', () => {
  const p = window.Alpine && globalThis.Alpine.store('player');
  if (p && p.session) {
    const headers = { 'Content-Type': 'application/json' };
    const csrf = csrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
    fetch(`/api/session/${p.session.id}/close`, {
      method: 'POST', keepalive: true, credentials: 'same-origin',
      headers,
      body: JSON.stringify({ currentTime: p.time, timeListened: p.listened, duration: p.duration }),
    });
  }
});

document.addEventListener('keydown', e => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName) || e.target.isContentEditable) return;
  if (document.querySelector('.ui-modal')) return;  // don't drive the player behind an open dialog
  const p = globalThis.Alpine.store('player');
  if (!p.active) return;
  if (e.code === 'Space') { e.preventDefault(); p.toggle(); }
  if (e.code === 'ArrowLeft') p.jump(-10);
  if (e.code === 'ArrowRight') p.jump(30);
});
}

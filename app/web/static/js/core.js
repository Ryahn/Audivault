/* Audivault web UI: Alpine.js store, router, API client, player and views. */

const TOKEN_KEY = 'abstream_token'; // legacy localStorage key — cleared on login/logout
const LIB_KEY = 'abstream_library';
const THEME_KEY = 'abstream_theme';

export function csrfToken() {
  const m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : '';
}

/* ------------------------------------------------------------------ helpers */

export const fmt = {
  duration(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m`;
    return `${sec}s`;
  },
  span(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
    if (d) return h ? `${d}d ${h}h` : `${d}d`;
    return this.duration(sec);
  },
  clock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  },
  parseClock(str) {
    const parts = String(str).trim().split(':').map(Number);
    if (parts.some(isNaN)) return null;
    return parts.reduce((acc, v) => acc * 60 + v, 0);
  },
  size(bytes) {
    if (!bytes) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(u.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / 1024 ** i).toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
  },
  date(ms) { return ms ? new Date(ms).toLocaleDateString() : ''; },
  datetime(ms) { return ms ? new Date(ms).toLocaleString() : ''; },
  b64(s) { return btoa(unescape(encodeURIComponent(s))); },
};
export function splitNames(str) {
  return String(str || '').split(/\s*[&;]\s*|\s*,\s*/).map(s => s.trim()).filter(Boolean);
}
export function splitList(str) {
  return String(str || '').split(/\s*,\s*/).map(s => s.trim()).filter(Boolean);
}

/* Client-side copy of the server's naming template renderer for live previews. */
export function renderTemplate(template, v) {
  const clean = s => String(s || '').replace(/[\/\\|]/g, '-').replace(/:/g, ' -').replace(/[<>"?*\x00-\x1f]/g, '').replace(/\s+/g, ' ').trim().replace(/[. ]+$/, '');
  const vals = Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clean(x)]));
  let out = template.replace(/\[([^\[\]]*)\]/g, (_, body) => {
    const tokens = [...body.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    if (tokens.some(t => !vals[t])) return '';
    return body.replace(/\{(\w+)\}/g, (_, t) => vals[t] || '');
  });
  out = out.replace(/\{(\w+)\}/g, (_, t) => vals[t] || '');
  return out.split('/').map(clean).filter(Boolean).join('/');
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}


export function deviceId() {
  let id = localStorage.getItem('abstream_device');
  if (!id) { id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random(); localStorage.setItem('abstream_device', id); }
  return id;
}

/* ------------------------------------------------------------------ API client */

let refreshing = null;

async function refreshToken() {
  if (!refreshing) {
    refreshing = fetch('/auth/refresh', { method: 'POST', credentials: 'same-origin' })
      .then(async r => {
        if (!r.ok) return false;
        // Access token is refreshed into an httpOnly cookie; clear any legacy localStorage token.
        localStorage.removeItem(TOKEN_KEY);
        return true;
      })
      .catch(() => false)
      .finally(() => setTimeout(() => { refreshing = null; }, 0));
  }
  return refreshing;
}

export async function api(method, path, body, retry = true) {
  const headers = {};
  const csrf = csrfToken();
  if (csrf && !['GET', 'HEAD'].includes(method.toUpperCase())) headers['X-CSRF-Token'] = csrf;
  let payload = body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  if (res.status === 401 && retry && !path.startsWith('/login')) {
    if (await refreshToken()) return api(method, path, body, false);
    globalThis.Alpine.store('app').logout(false);
    throw new Error('Session expired');
  }
  const ctype = res.headers.get('content-type') || '';
  const data = ctype.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof data === 'string' ? (data || res.statusText) : (data.error || data.detail || res.statusText));
  return data;
}

export function libraryBusy(libraryId) {
  return globalThis.Alpine.store('app').runningTasks.some(t => {
    if (!['scan', 'organize', 'duplicates'].includes(t.kind)) return false;
    return !t.libraryId || t.libraryId === libraryId;
  });
}

/* One refresh after changes go quiet, and not while a job is still moving files. */
export function onLibraryChange(view, reload) {
  let timer = null;
  const settle = () => {
    const libraryId = globalThis.Alpine.store('app').libraryId;
    if (libraryBusy(libraryId)) {
      timer = setTimeout(settle, 1000);
      return;
    }
    reload();
  };
  const handler = e => {
    if (e.detail.libraryId !== globalThis.Alpine.store('app').libraryId) return;
    clearTimeout(timer);
    timer = setTimeout(settle, 2000);
  };
  window.addEventListener('abstream-library-changed', handler);
  view.$cleanup = () => window.removeEventListener('abstream-library-changed', handler);
}

export function registerApp(Alpine) {
  Alpine.store('app', {
    ready: false,
    initialized: true,
    user: null,
    libraries: [],
    libraryId: localStorage.getItem(LIB_KEY) || '',
    route: { name: 'home', params: [], query: {} },
    routeKey: 0,
    toasts: [],
    tasks: [],
    theme: localStorage.getItem(THEME_KEY) || 'dark',
    search: '',
    modal: null,          // {type, ...data}
    duplicateCount: 0,
    pluginNav: [],
    progressMap: {},

    get isAdmin() { return this.user && ['root', 'admin'].includes(this.user.type); },
    get canUpdate() { return this.user && (this.isAdmin || this.user.permissions?.update); },
    get canDelete() { return this.user && (this.isAdmin || this.user.permissions?.delete); },
    get library() { return this.libraries.find(l => l.id === this.libraryId) || this.libraries[0] || null; },
    get runningTasks() { return this.tasks.filter(t => t.status === 'running'); },

    async init() {
      document.documentElement.dataset.theme = this.theme;
      window.addEventListener('hashchange', () => this.parseRoute());
      this.parseRoute();
      try {
        const status = await fetch('/status').then(r => r.json());
        this.initialized = status.isInit;
      } catch (e) { /* offline */ }
      if (this.initialized) {
        try {
          const data = await api('POST', '/api/authorize');
          await this.setSession(data);
        } catch (e) {
          localStorage.removeItem(TOKEN_KEY);
        }
      }
      this.ready = true;
      this.pollTasks();
    },

    async setSession(data) {
      this.user = data.user;
      // Prefer httpOnly cookie sessions; drop legacy localStorage access tokens.
      localStorage.removeItem(TOKEN_KEY);
      this.indexProgress(data.user.mediaProgress || []);
      await this.loadLibraries();
      if (!this.libraryId || !this.libraries.some(l => l.id === this.libraryId)) {
        this.setLibrary(data.userDefaultLibraryId || (this.libraries[0] && this.libraries[0].id) || '');
      }
      this.connectEvents();
      this.loadDuplicateCount();
      this.loadPluginNav();
    },

    async loadPluginNav() {
      if (!this.isAdmin) { this.pluginNav = []; return; }
      try { this.pluginNav = (await api('GET', '/api/abstream/plugins')).nav || []; }
      catch (e) { this.pluginNav = []; }
    },

    async loadDuplicateCount() {
      if (!this.isAdmin) return;
      try { this.duplicateCount = (await api('GET', '/api/abstream/duplicates/count')).count; } catch (e) { }
    },

    /* ---------------- real-time events (Server-Sent Events) ---------------- */
    events: null,
    eventsConnecting: false,
    eventsRetry: 0,
    eventsConnected: false,

    async connectEvents() {
      if (this.events || this.eventsConnecting || !this.user) return;
      this.eventsConnecting = true;   // set before awaiting so concurrent callers can't open a second stream
      try {
        const { ticket } = await api('POST', '/api/abstream/events/ticket');   // refreshes the token if needed
        const es = new EventSource(`/api/abstream/events?ticket=${encodeURIComponent(ticket)}`);
        this.events = es;
        es.onopen = () => { this.eventsConnected = true; this.eventsRetry = 0; this.resyncTasks(); };
        es.onmessage = e => { try { this.onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
        es.onerror = () => {
          // The ticket is single-use, so never let EventSource retry by itself
          es.close();
          if (this.events === es) this.events = null;
          this.eventsConnected = false;
          this.scheduleReconnect();
        };
      } catch (e) {
        this.scheduleReconnect();
      } finally {
        this.eventsConnecting = false;
      }
    },

    scheduleReconnect() {
      if (!this.user) return;
      const delay = Math.min(30000, 2000 * 2 ** this.eventsRetry++);
      setTimeout(() => this.connectEvents(), delay);
    },

    disconnectEvents() {
      if (this.events) this.events.close();
      this.events = null;
      this.eventsConnected = false;
    },

    async resyncTasks() {
      try { this.tasks = (await api('GET', '/api/abstream/tasks')).tasks; } catch (e) { }
    },

    onEvent(ev) {
      if (ev.type === 'task') {
        const t = ev.task;
        const i = this.tasks.findIndex(x => x.id === t.id);
        if (i >= 0) this.tasks.splice(i, 1, t); else this.tasks.unshift(t);
        if (t.status !== 'running') this.notifyTaskDone(t);
      } else if (ev.type === 'library_changed') {
        window.dispatchEvent(new CustomEvent('abstream-library-changed', { detail: ev }));
      } else if (ev.type === 'import_changed') {
        window.dispatchEvent(new CustomEvent('abstream-import-changed', { detail: ev }));
        if (ev.new > 0 && this.isAdmin) {
          const lib = this.libraries.find(l => l.id === ev.libraryId);
          this.toast(`${ev.new} new item${ev.new === 1 ? '' : 's'} in the import folder${lib ? ` for ${lib.name}` : ''}`, 'info',
            this.route.name === 'import' ? null : { label: 'Open Import', href: '#/import' });
        }
      } else if (ev.type === 'duplicates_runtimes') {
        window.dispatchEvent(new CustomEvent('abstream-duplicates-changed'));
      } else if (ev.type === 'duplicates_found') {
        this.duplicateCount = ev.count;
        window.dispatchEvent(new CustomEvent('abstream-duplicates-changed'));
        this.toast(`Found ${ev.new} new set${ev.new === 1 ? '' : 's'} of duplicate books`, 'warning',
          this.route.name === 'duplicates' ? null : { label: 'Review', href: '#/duplicates' });
      } else if (ev.type === 'trash_changed') {
        window.dispatchEvent(new CustomEvent('abstream-trash-changed'));
      } else if (ev.type === 'activity') {
        window.dispatchEvent(new CustomEvent('abstream-activity', { detail: ev.entry }));
      }
    },

    notifyTaskDone(t) {
      if (t.status === 'failed') return this.toast(`${t.title} failed: ${t.error || 'unknown error'}`, 'error');
      const r = t.result || {};
      if (t.kind === 'scan') {
        const changed = (r.added || 0) + (r.updated || 0) + (r.missing || 0) + (r.organized || 0);
        if (t.quiet && !changed) return;  // background scans only speak up when something changed
        const parts = [r.added && `${r.added} added`, r.updated && `${r.updated} updated`, r.missing && `${r.missing} missing`].filter(Boolean);
        if (r.organized) parts.push(`${r.organized} organized`);
        const label = r.partial ? 'Library updated from folder changes' : `${t.title} finished`;
        this.toast(`${label}: ${parts.join(', ') || 'no changes'}`, r.missing ? 'warning' : 'success');
        if (changed) this.loadLibraries().catch(() => { });
      } else if (t.kind === 'organize') {
        const failed = (r.failed || []).length;
        const parts = [`${r.moved || 0} folder(s) renamed`];
        if (r.removed) parts.push(`${r.removed} leftover entr${r.removed === 1 ? 'y' : 'ies'} removed`);
        if (failed) parts.push(`${failed} failed`);
        this.toast(`Organize finished: ${parts.join(', ')}`, failed ? 'warning' : 'success');
      } else if (t.kind === 'match') {
        this.toast(`Quick match finished: ${r.matched || 0} book(s) updated`, 'success');
      } else if (t.kind === 'duplicates' && r.batch) {
        const failed = (r.failed || []).length;
        window.dispatchEvent(new CustomEvent('abstream-trash-changed'));
        this.toast(`Resolved ${r.resolved} duplicate group(s)` + (failed ? `, ${failed} failed` : '') + ` (${r.copied || 0} file(s) copied, ${r.replaced || 0} replaced)`, failed ? 'warning' : 'success');
        this.loadDuplicateCount();
        window.dispatchEvent(new CustomEvent('abstream-duplicates-changed'));
      } else if (t.kind === 'duplicates') {
        const extra = (r.copied || r.replaced) ? ` (${r.copied || 0} file(s) copied, ${r.replaced || 0} replaced)` : '';
        this.toast(`Duplicates resolved: kept ${r.kept}, ${(r.disposed || []).length} copy(ies) ${r.mode === 'delete' ? 'deleted' : 'moved to trash'}${extra}`, 'success');
        this.loadDuplicateCount();
        window.dispatchEvent(new CustomEvent('abstream-duplicates-changed'));
      } else if (t.kind === 'import' || t.kind === 'import-batch') {
        const n = (r.imported || []).length, rv = (r.review || []).length, f = (r.failed || []).length;
        const s = Array.isArray(r.skipped) ? r.skipped.length : 0;
        if (!n && !rv && !f && !s) return;
        const parts = [n && `imported ${n} book(s)`, s && `${s} already in the library`, rv && `${rv} need(s) review`, f && `${f} failed`].filter(Boolean).join(', ');
        const label = t.kind === 'import-batch' ? 'Import' : 'Auto-import';
        this.toast(`${label}: ${parts}`, rv || f ? 'warning' : 'success', rv || f ? { label: 'Open Import', href: '#/import' } : null);
        if (t.kind === 'import-batch') {
          window.dispatchEvent(new CustomEvent('abstream-import-batch', { detail: { libraryId: t.libraryId, result: r } }));
        }
        if (n) this.loadLibraries().catch(() => { });
      } else if (t.kind === 'trash') {
        window.dispatchEvent(new CustomEvent('abstream-trash-changed'));
        const lockedNote = r.keptLocked ? ` (${r.keptLocked} batch(es) in locked folders kept)` : '';
        if (r.removed) this.toast(`${r.everything ? 'Emptied the trash' : `Emptied trash older than ${r.days} day(s)`}: ${r.removed} batch(es), ${fmt.size(r.freed)} freed${lockedNote}`, 'success');
        else if (!t.quiet && r.keptLocked) this.toast(`Nothing deleted: the remaining trash is in locked folders`, 'info');
        else if (!t.quiet) this.toast('The trash was already empty', 'info');
      } else if (t.kind === 'chapters') {
        this.toast(`Chapters refreshed: ${r.updated || 0} of ${r.withCue || 0} book(s) with a .cue file changed`, 'success');
      } else if (t.kind === 'series-match') {
        const failed = (r.failed || []).length + ((r.organized && r.organized.failed) || []).length;
        const moved = (r.organized || {}).moved || 0;
        const parts = [`${r.matched || 0} matched`];
        if (moved) parts.push(`${moved} folder(s) renamed`);
        if (failed) parts.push(`${failed} failed`);
        this.toast(`Series match finished: ${parts.join(', ')}`, failed ? 'warning' : 'success');
      } else if (!t.quiet) {
        this.toast(`${t.title} finished`, 'success');
      }
    },

    indexProgress(list) {
      const map = {};
      for (const p of list) map[p.libraryItemId] = p;
      this.progressMap = map;
    },

    async refreshUser() {
      const me = await api('GET', '/api/me');
      this.user = { ...this.user, ...me };
      this.indexProgress(me.mediaProgress || []);
    },

    async loadLibraries() {
      const data = await api('GET', '/api/libraries');
      this.libraries = data.libraries;
    },

    setLibrary(id) {
      this.libraryId = id;
      localStorage.setItem(LIB_KEY, id);
    },

    async logout(callServer = true) {
      if (callServer) { try { await fetch('/logout', { method: 'POST', credentials: 'same-origin' }); } catch (e) { } }
      Alpine.store('player').stop();
      this.disconnectEvents();
      localStorage.removeItem(TOKEN_KEY);
      this.user = null;
      location.hash = '#/';
    },

    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      localStorage.setItem(THEME_KEY, this.theme);
      document.documentElement.dataset.theme = this.theme;
    },

    parseRoute() {
      const hash = location.hash.replace(/^#\/?/, '');
      const [path, qs] = hash.split('?');
      const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
      const query = Object.fromEntries(new URLSearchParams(qs || ''));
      this.route = { name: parts[0] || 'home', params: parts.slice(1), query };
      this.routeKey++;
      this.modal = null;
      window.scrollTo(0, 0);
    },

    go(path) { location.hash = '#/' + path.replace(/^\//, ''); },

    toast(message, kind = 'info', action = null) {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, message, kind, action });
      setTimeout(() => this.dismissToast(id), kind === 'error' || action ? 8000 : 4500);
    },
    dismissToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },

    async pollTasks() {
      if (this.user) {
        try { this.tasks = (await api('GET', '/api/abstream/tasks')).tasks; } catch (e) { }
      }
      // Live updates arrive over the event stream; polling is only a fallback when it is down
      setTimeout(() => this.pollTasks(), this.eventsConnected ? 60000 : (this.runningTasks.length ? 3000 : 15000));
    },

    async waitForTask(task) {
      while (true) {
        const t = await api('GET', `/api/abstream/tasks/${task.id}`);
        if (t.status !== 'running') return t;
        await new Promise(r => setTimeout(r, 1200));
      }
    },

    cover(item, thumb = false) {
      if (!item || !item.media || !item.media.coverPath) return null;
      const w = thumb ? '&width=300' : '';
      return `/api/items/${item.id}/cover?ts=${item.updatedAt}${w}`;
    },
    progressOf(itemId) { return this.progressMap[itemId] || null; },

    doSearch() {
      const q = this.search.trim();
      if (!q || !this.library) return;
      this.go(`library/${this.library.id}?tab=books&q=${encodeURIComponent(q)}`);
    },
  });

  Alpine.store('dialogs', {
    stack: [],
    seq: 0,
    open(opts) {
      return new Promise(resolve => {
        this.stack.push({
          id: ++this.seq, kind: 'confirm', title: '', message: '', detail: '', confirmText: 'OK', cancelText: 'Cancel', danger: false,
          input: false, inputLabel: '', inputValue: '', ...opts, resolve,
        });
      });
    },
    accept(dialog) {
      if (!this.stack.includes(dialog)) return;
      if (dialog.input) {
        const value = String(dialog.inputValue || '').trim();
        if (!value) return;
        this.close(dialog, value);
        return;
      }
      this.close(dialog, true);
    },
    close(dialog, result) {
      if (!this.stack.includes(dialog)) return;   // already closed: ignore double clicks
      this.stack = this.stack.filter(d => d !== dialog);
      dialog.resolve(result);
    },
  });

}

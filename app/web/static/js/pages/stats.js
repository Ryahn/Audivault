import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

const STAT_WINDOWS = [
  { id: '24h', short: '24h', label: 'Last 24 hours' },
  { id: '3d', short: '3d', label: 'Last 3 days' },
  { id: '7d', short: '7d', label: 'Last 7 days' },
  { id: '1m', short: '1m', label: 'Last 30 days' },
  { id: '3m', short: '3m', label: 'Last 90 days' },
  { id: '6m', short: '6m', label: 'Last 6 months' },
  { id: '12m', short: '12m', label: 'Last year' },
  { id: 'all', short: 'All', label: 'All time' },
];

const EMPTY_WINDOW = {
  time: 0, booksFinished: 0, booksListened: 0,
  genres: [], authors: [], narrators: [], books: [], dayOfWeek: [],
};

export function statsView() {
  return {
    tab: 'library',
    windowId: 'all',
    windows: STAT_WINDOWS,
    libraryStats: null,
    listening: null,
    loading: true,
    destroy() { this.$cleanup && this.$cleanup(); },
    async init() {
      const app = globalThis.Alpine.store('app');
      this.tab = app.route.params[0] === 'you' ? 'you' : 'library';
      onLibraryChange(this, () => this.load());
      await this.load();
    },
    setTab(tab) {
      this.tab = tab;
      history.replaceState(null, '', `#/stats/${tab}`);
    },
    async load() {
      const app = globalThis.Alpine.store('app');
      this.loading = true;
      const listening = api('GET', '/api/me/listening-stats').catch(e => { app.toast(e.message, 'error'); return null; });
      const library = app.library
        ? api('GET', `/api/libraries/${app.library.id}/stats`).catch(e => { app.toast(e.message, 'error'); return null; })
        : Promise.resolve(null);
      const [listenResult, libraryResult] = await Promise.all([listening, library]);
      this.listening = listenResult;
      this.libraryStats = libraryResult;
      this.loading = false;
    },
    current() {
      const windows = this.listening && this.listening.windows;
      return (windows && windows[this.windowId]) || EMPTY_WINDOW;
    },
    periodEmpty() { return !this.current().time; },
    pct(value, rows, field) {
      let max = 0;
      for (const row of rows || []) max = Math.max(max, Number(row[field]) || 0);
      const n = Number(value) || 0;
      if (!max || !n) return 0;
      return Math.max(4, (n / max) * 100);
    },
    countLabel(n, noun) { return `${n} ${noun}${n === 1 ? '' : 's'}`; },
    libraryFilter(group, value) {
      const id = globalThis.Alpine.store('app').libraryId;
      const filter = `${group}.${fmt.b64(value)}`;
      return `#/library/${id}?tab=books&filter=${encodeURIComponent(filter)}&label=${encodeURIComponent(value)}`;
    },
    cover(id) { return id ? `/api/items/${id}/cover` : ''; },
    dayLabel(day) {
      return { Monday: 'Mon', Tuesday: 'Tue', Wednesday: 'Wed', Thursday: 'Thu', Friday: 'Fri', Saturday: 'Sat', Sunday: 'Sun' }[day] || day;
    },
  };
}

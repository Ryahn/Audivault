import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function seriesView() {
  return {
    series: null, loading: true, editing: false, name: '', finishing: false,
    async init() {
      try { this.series = await api('GET', `/api/series/${globalThis.Alpine.store('app').route.params[0]}?include=progress`); this.name = this.series.name; }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.loading = false;
    },
    async save() {
      try { this.series = await api('PATCH', `/api/series/${this.series.id}`, { name: this.name }); this.editing = false; }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async setFinished(val) {
      if (this.finishing) return;
      const app = globalThis.Alpine.store('app');
      const ids = (this.series.books || []).map(b => b.id);
      this.finishing = true;
      try {
        await markBooksFinished(ids, val);
        this.series.progress = {
          ...(this.series.progress || {}),
          libraryItemIds: ids,
          libraryItemIdsFinished: val ? ids.slice() : [],
          isFinished: val && ids.length > 0,
        };
        ids.forEach(id => paintCards(`book:${id}`));
        app.toast(val ? 'Series marked as finished' : 'Series marked as not finished', 'success');
      } catch (e) { app.toast(e.message, 'error'); }
      this.finishing = false;
    },
    playNext() {
      const app = globalThis.Alpine.store('app');
      const next = this.series.books.find(b => !(app.progressOf(b.id) || {}).isFinished) || this.series.books[0];
      if (next) globalThis.Alpine.store('player').play(next.id);
    },
    matchSeries() {
      const view = this;
      globalThis.Alpine.store('app').modal = {
        type: 'series-match',
        seriesId: this.series.id,
        onDone() { return view.reload(); },
      };
    },
    async reload() {
      try {
        this.series = await api('GET', `/api/series/${this.series.id}?include=progress`);
        this.name = this.series.name;
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
  };
}

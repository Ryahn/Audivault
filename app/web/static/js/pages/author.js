import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function authorView() {
  return {
    author: null, loading: true, editing: false, form: {}, matching: false, expanded: false,
    async init() { await this.load(); },
    async load() {
      try {
        this.author = await api('GET', `/api/authors/${globalThis.Alpine.store('app').route.params[0]}?include=items,series`);
        this.form = { name: this.author.name, description: this.author.description || '', asin: this.author.asin || '' };
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.loading = false;
    },
    get standalone() {
      const inSeries = new Set(this.author.series.flatMap(s => s.items.map(i => i.id)));
      return this.author.libraryItems.filter(i => !inSeries.has(i.id));
    },
    async save() {
      const app = globalThis.Alpine.store('app');
      try {
        const r = await api('PATCH', `/api/authors/${this.author.id}`, this.form);
        this.editing = false;
        if (r.merged) { app.toast('Merged into existing author', 'success'); app.go(`author/${r.author.id}`); return; }
        await this.load();
      } catch (e) { app.toast(e.message, 'error'); }
    },
    async match() {
      const app = globalThis.Alpine.store('app');
      this.matching = true;
      try {
        const r = await api('POST', `/api/authors/${this.author.id}/match`, { q: this.form.name, asin: this.form.asin || undefined });
        app.toast(r.updated ? 'Author info updated' : 'No match found on Audible', r.updated ? 'success' : 'warning');
        await this.load();
      } catch (e) { app.toast(e.message, 'error'); }
      this.matching = false;
    },
    image() { return this.author.imagePath ? `/api/authors/${this.author.id}/image?ts=${this.author.updatedAt}` : null; },
  };
}

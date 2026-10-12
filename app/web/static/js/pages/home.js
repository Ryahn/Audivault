import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function homeView() {
  return {
    shelves: [], loading: true,
    destroy() { this.$cleanup && this.$cleanup(); },
    async init() {
      onLibraryChange(this, () => this.load());
      await this.load();
    },
    async load() {
      const app = globalThis.Alpine.store('app');
      if (!app.library) { this.loading = false; return; }
      try { this.shelves = await api('GET', `/api/libraries/${app.library.id}/personalized?limit=14`); }
      catch (e) { app.toast(e.message, 'error'); }
      this.loading = false;
    },
  };
}

import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function organizeItemModal() {
  return {
    plan: null, busy: false,
    async init() {
      const item = globalThis.Alpine.store('app').modal.item;
      try { this.plan = await api('GET', `/api/abstream/items/${item.id}/organize`); } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async apply() {
      const app = globalThis.Alpine.store('app');
      const m = app.modal;
      this.busy = true;
      try {
        const r = await api('POST', `/api/abstream/items/${m.item.id}/organize`);
        const done = r.status === 'moved' ? 'Folder renamed' : r.status === 'removed' ? 'Leftover entry removed' : '';
        app.toast(done || `Not moved: ${r.detail || r.status}`, done ? 'success' : 'warning');
        if (m.onDone) await m.onDone();
        app.modal = null;
      } catch (e) { app.toast(e.message, 'error'); }
      this.busy = false;
    },
  };
}

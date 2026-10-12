import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function itemView() {
  return {
    item: null, loading: true, tab: 'chapters', expanded: false, sessions: [],
    async init() { await this.load(); },
    async load() {
      const app = globalThis.Alpine.store('app');
      try {
        this.item = await api('GET', `/api/items/${app.route.params[0]}?expanded=1&include=progress`);
        if (this.item.userMediaProgress) app.progressMap = { ...app.progressMap, [this.item.id]: this.item.userMediaProgress };
        if (!this.item.media.chapters.length) this.tab = 'files';
      } catch (e) { app.toast(e.message, 'error'); }
      this.loading = false;
    },
    get md() { return this.item.media.metadata; },
    get progress() { return globalThis.Alpine.store('app').progressOf(this.item.id); },
    get pct() { const p = this.progress; return p ? Math.round((p.isFinished ? 1 : p.progress) * 100) : 0; },
    play(at = null) { globalThis.Alpine.store('player').play(this.item.id, at); },
    async setFinished(val) {
      const app = globalThis.Alpine.store('app');
      const p = await api('PATCH', `/api/me/progress/${this.item.id}`, { isFinished: val });
      app.progressMap = { ...app.progressMap, [this.item.id]: p };
      app.toast(val ? 'Marked as finished' : 'Marked as not finished', 'success');
    },
    async resetProgress() {
      const app = globalThis.Alpine.store('app');
      const p = this.progress;
      if (!p) return;
      if (!await ui.confirm({ title: 'Discard progress?', message: `Your listening position in "${this.md.title}" will be reset to the beginning.`, confirmText: 'Discard progress', danger: true })) return;
      await api('DELETE', `/api/me/progress/${p.id}`);
      const map = { ...app.progressMap }; delete map[this.item.id]; app.progressMap = map;
    },
    edit(tab = 'details') { globalThis.Alpine.store('app').modal = { type: 'edit', item: this.item, tab, onSave: () => this.load() }; },
    organize() { globalThis.Alpine.store('app').modal = { type: 'organize-item', item: this.item, onDone: () => this.load() }; },
    async quickMatch() {
      const app = globalThis.Alpine.store('app');
      app.toast('Searching providers…');
      try {
        const r = await api('POST', `/api/items/${this.item.id}/match`, { overrideDefaults: false });
        app.toast(r.updated ? 'Book details updated' : (r.warning || 'No changes'), r.updated ? 'success' : 'warning');
        this.item = r.libraryItem;
      } catch (e) { app.toast(e.message, 'error'); }
    },
    async rescan() {
      const app = globalThis.Alpine.store('app');
      try { const r = await api('POST', `/api/items/${this.item.id}/scan`); app.toast(`Rescan: ${r.result.toLowerCase()}`, 'success'); await this.load(); }
      catch (e) { app.toast(e.message, 'error'); }
    },
    async remove(hard) {
      const app = globalThis.Alpine.store('app');
      const ok = await ui.confirm(hard
        ? { title: 'Delete files from disk?', message: `This permanently deletes "${this.md.title}" and every file in its folder. This cannot be undone.`, detail: this.item.path, confirmText: 'Delete files', danger: true }
        : { title: 'Remove from library?', message: `"${this.md.title}" is removed from the library database. Its files stay on disk, so a rescan will add it back.`, confirmText: 'Remove', danger: true });
      if (!ok) return;
      try {
        await api('DELETE', `/api/items/${this.item.id}?hard=${hard ? 1 : 0}`);
        app.toast(hard ? 'Files deleted' : 'Removed from library', 'success');
        app.go(`library/${this.item.libraryId}`);
      } catch (e) { app.toast(e.message, 'error'); }
    },
    async download() {
      const app = globalThis.Alpine.store('app');
      try {
        const data = await api('GET', `/api/items/${this.item.id}/download-url`);
        location.href = data.url;
      } catch (e) { app.toast(e.message, 'error'); }
    },
    get currentChapter() {
      const p = globalThis.Alpine.store('player');
      if (!p.session || p.session.libraryItemId !== this.item.id) return -1;
      return p.chapterIndex;
    },
  };
}

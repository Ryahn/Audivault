import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function organizeView() {
  return {
    libId: '', template: '', plans: [], loading: false, selected: [], showAll: false, running: null,
    init() {
      this.libId = globalThis.Alpine.store('app').libraryId;
      this.resetTemplate();
      this.preview();
    },
    get lib() { return globalThis.Alpine.store('app').libraries.find(l => l.id === this.libId); },
    resetTemplate() { this.template = this.lib ? this.lib.settings.namingTemplate : '{author}/[{series}/][{sequence} - ]{title}'; },
    async preview() {
      this.loading = true;
      try {
        const p = new URLSearchParams({ template: this.template, all: this.showAll ? 1 : 0 });
        this.plans = (await api('GET', `/api/abstream/libraries/${this.libId}/organize?${p}`)).plans;
        this.selected = this.plans.filter(p => this.actionable(p)).map(p => p.id);
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.loading = false;
    },
    get okCount() { return this.plans.filter(p => p.status === 'ok').length; },
    get staleCount() { return this.plans.filter(p => p.status === 'stale').length; },
    get skippedCount() { return this.plans.filter(p => !this.actionable(p)).length; },
    get actionableCount() { return this.plans.filter(p => this.actionable(p)).length; },
    actionable(p) { return p.status === 'ok' || p.status === 'stale'; },
    statusLabel(p) {
      if (p.status === 'ok' && p.caseOnly) return 'capitalization';
      return { ok: 'ready', unchanged: 'unchanged', conflict: 'conflict', missing: 'missing', stale: 'leftover', error: 'error' }[p.status] || p.status;
    },
    toggleAll(on) { this.selected = on ? this.plans.filter(p => this.actionable(p)).map(p => p.id) : []; },
    async apply() {
      const app = globalThis.Alpine.store('app');
      if (!this.selected.length) return;
      const chosen = this.plans.filter(p => this.selected.includes(p.id));
      const renames = chosen.filter(p => p.status === 'ok').length;
      const leftovers = chosen.filter(p => p.status === 'stale').length;
      const lines = [];
      if (renames) lines.push(`${renames} book folder(s) will be moved or renamed using this template.`);
      if (leftovers) lines.push(`${leftovers} leftover library entr${leftovers === 1 ? 'y' : 'ies'} will be removed. The files stay where they are, and listening progress is kept on the copy already in the library.`);
      if (!await ui.confirm({ title: leftovers && !renames ? 'Remove leftover entries?' : 'Rename folders on disk?', message: lines.join(' '), detail: this.template, confirmText: leftovers && !renames ? 'Remove leftovers' : 'Rename folders' })) return;
      try {
        const r = await api('POST', `/api/abstream/libraries/${this.libId}/organize`, { itemIds: this.selected, template: this.template });
        this.running = r.task;
        await app.waitForTask(r.task);   // the result toast arrives over the event stream
        this.running = null;
        await this.preview();
      } catch (e) { app.toast(e.message, 'error'); this.running = null; }
    },
    async saveTemplate() {
      try {
        await api('PATCH', `/api/libraries/${this.libId}`, { settings: { namingTemplate: this.template } });
        await globalThis.Alpine.store('app').loadLibraries();
        globalThis.Alpine.store('app').toast('Template saved as library default', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
  };
}

import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

/* ------------------------------------------------------------------ duplicates */

export function duplicatesView() {
  return {
    groups: [], loading: true, keep: {}, resolving: null, busyKey: null, batchBusy: false,
    destroy() { window.removeEventListener('abstream-duplicates-changed', this._onChange); },
    async init() {
      this._onChange = () => { if (!this.resolving) this.load(true); };
      window.addEventListener('abstream-duplicates-changed', this._onChange);
      await this.load();
    },
    async load(silent = false) {
      if (!silent) this.loading = true;
      try {
        const d = await api('GET', '/api/abstream/duplicates');
        this.groups = d.groups;
        globalThis.Alpine.store('app').duplicateCount = d.count;
        // Forget choices for groups that are gone
        const keys = new Set(d.groups.map(g => g.key));
        for (const k of Object.keys(this.keep)) if (!keys.has(k)) delete this.keep[k];
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.loading = false;
    },
    cover(c) { return `/api/items/${c.id}/cover?ts=${c.updatedAt}`; },
    selectedGroups() { return this.groups.filter(g => this.keep[g.key]); },
    /* Why a group can't be resolved with the current choice (locked copies can never be discarded) */
    lockBlock(g) {
      const locked = g.items.filter(c => c.locked);
      if (locked.length > 1) return 'More than one copy is in a locked folder. Locked copies are never discarded, so unlock all but one folder to resolve this group.';
      const keepId = this.keep[g.key];
      if (keepId && locked.length && locked[0].id !== keepId) return `The copy in ${locked[0].folderPath} is locked and can't be discarded. Keep that copy instead.`;
      return null;
    },
    selectRecommended() {
      const keep = { ...this.keep };
      for (const g of this.groups) if (g.recommendation) keep[g.key] = g.recommendation.id;
      this.keep = keep;
    },
    /* "Same audio saved twice" fix: exclude the lower-quality duplicate track(s) of one copy */
    async applyFix(g) {
      const app = globalThis.Alpine.store('app');
      const copy = g.items.find(c => c.id === g.recommendation.alternativeId);
      const fix = copy && copy.analysis.fix;
      if (!fix) return;
      const ok = await ui.confirm({
        title: 'Exclude duplicate track?',
        message: `Only ${fix.keepFile} (${fix.kbps} kbps) will play for this copy. The excluded file stays on disk and can be included again from the book's Edit → Tracks tab.`,
        detail: fix.excludeFiles.join('\n'), confirmText: 'Exclude',
      });
      if (!ok) return;
      this.busyKey = g.key;
      try {
        const item = await api('GET', `/api/items/${copy.id}?expanded=1`);
        const already = item.media.audioFiles.filter(a => a.exclude).map(a => a.ino);
        await api('POST', `/api/abstream/items/${copy.id}/audio-files`, { exclude: [...new Set([...already, ...fix.exclude])] });
        app.toast(`Excluded ${fix.excludeFiles.length} duplicate track(s)`, 'success');
        await this.load();
      } catch (e) { app.toast(e.message, 'error'); }
      this.busyKey = null;
    },
    async resolveSelected() {
      const app = globalThis.Alpine.store('app');
      const blocked = this.selectedGroups().filter(g => this.lockBlock(g));
      const chosen = this.selectedGroups().filter(g => !this.lockBlock(g));
      if (blocked.length) app.toast(`Skipping ${blocked.length} group(s) where a locked copy would be discarded`, 'warning');
      if (!chosen.length) return;
      const plans = chosen.map(g => ({ keepId: this.keep[g.key], discardIds: g.items.filter(c => c.id !== this.keep[g.key]).map(c => c.id) }));
      const discards = plans.reduce((n, p) => n + p.discardIds.length, 0);
      const ok = await ui.confirm({
        title: `Resolve ${plans.length} group(s)?`,
        message: `${discards} copy(ies) will be moved to the .abstream-trash folder in their library folder. Missing sidecar files (metadata, cue sheets from identical audio, ebooks) and larger metadata files are brought over to the kept copies, and listening history moves with them.`,
        confirmText: 'Resolve and trash',
      });
      if (!ok) return;
      this.batchBusy = true;
      try {
        const r = await api('POST', '/api/abstream/duplicates/resolve-batch', { plans, mode: 'trash' });
        const t = await app.waitForTask(r.task);   // result toast arrives over the event stream
        if (t.status === 'failed' && !app.eventsConnected) app.toast(t.error, 'error');
        this.keep = {};
        await this.load();
      } catch (e) { app.toast(e.message, 'error'); }
      this.batchBusy = false;
    },
    async notDuplicates(g) {
      const ok = await ui.confirm({ title: 'Not duplicates?', message: `These ${g.items.length} books will be hidden from the duplicate list. If another copy appears later, the group shows up again.`, confirmText: 'Hide group' });
      if (!ok) return;
      try { await api('POST', '/api/abstream/duplicates/ignore', { itemIds: g.items.map(i => i.id) }); await this.load(); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    /* Opens the resolve dialog: one file comparison per copy being discarded */
    async openResolve(g) {
      const keepId = this.keep[g.key];
      if (!keepId) return;
      this.busyKey = g.key;
      try {
        const keep = g.items.find(i => i.id === keepId);
        const discards = [];
        for (const c of g.items.filter(i => i.id !== keepId)) {
          const cmp = await api('GET', `/api/abstream/duplicates/compare?keep=${encodeURIComponent(keepId)}&discard=${encodeURIComponent(c.id)}`);
          discards.push({ copy: c, cmp, files: cmp.files.map(f => ({ ...f })) });
        }
        this.resolving = { group: g, keep, discards, mode: 'trash', busy: false };
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.busyKey = null;
    },
    selectedCount(d) { return d.files.filter(f => f.selected).length; },
    async resolve() {
      const r = this.resolving;
      const app = globalThis.Alpine.store('app');
      if (r.mode === 'delete') {
        const ok = await ui.confirm({
          title: 'Delete permanently?',
          message: `${r.discards.length} copy(ies) of "${r.keep.title}" will be deleted from disk. This cannot be undone.`,
          detail: r.discards.map(d => d.copy.path).join('\n'), confirmText: 'Delete permanently', danger: true,
        });
        if (!ok) return;
      }
      r.busy = true;
      try {
        const body = {
          keepId: r.keep.id, mode: r.mode,
          discards: r.discards.map(d => ({
            id: d.copy.id,
            copy: d.files.filter(f => f.selected && f.status === 'missing').map(f => f.relPath),
            replace: d.files.filter(f => f.selected && f.status !== 'missing').map(f => f.relPath),
          })),
        };
        const res = await api('POST', '/api/abstream/duplicates/resolve', body);
        this.resolving = null;
        const t = await app.waitForTask(res.task);   // the result toast arrives over the event stream
        if (t.status === 'failed' && !app.eventsConnected) app.toast(t.error, 'error');
        await this.load();
      } catch (e) { app.toast(e.message, 'error'); r.busy = false; }
    },
    statusLabel(s) { return { missing: 'Only in this copy', larger: 'Larger here', smaller: 'Smaller here', same: 'Identical size' }[s] || s; },
  };
}

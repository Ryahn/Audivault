import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function importView() {
  return {
    libId: '', candidates: [], loading: false, error: '', importFolder: '', template: '',
    active: null, providers: [], provider: 'audible', results: [], searching: false, mode: 'move', stale: 0,
    filter: 'pending', batching: false,
    destroy() {
      window.removeEventListener('abstream-import-changed', this._onImport);
      window.removeEventListener('abstream-import-batch', this._onBatch);
    },
    async init() {
      const app = globalThis.Alpine.store('app');
      this.libId = app.libraryId;
      // Don't reload automatically: that would throw away metadata typed into the forms
      this._onImport = e => {
        if (e.detail.libraryId !== this.libId || this.batching) return;
        if (e.detail.count !== this.candidates.length) this.stale = e.detail.count;
      };
      this._onBatch = e => { if (e.detail.libraryId === this.libId) this.applyBatchResult(e.detail.result || {}); };
      window.addEventListener('abstream-import-changed', this._onImport);
      window.addEventListener('abstream-import-batch', this._onBatch);
      api('GET', '/api/search/providers').then(d => { this.providers = d.providers.books; });
      await this.load();
    },
    get lib() { return globalThis.Alpine.store('app').libraries.find(l => l.id === this.libId); },
    statusOf(c) { return (c.autoImport && c.autoImport.status) || ''; },
    get skippedCandidates() {
      return this.candidates.filter(c => this.statusOf(c) === 'skipped');
    },
    get visibleCandidates() {
      return this.candidates.filter(c => {
        const s = this.statusOf(c);
        if (this.filter === 'review') return s === 'review';
        if (this.filter === 'imported') return s === 'imported';
        if (this.filter === 'skipped') return s === 'skipped';
        if (this.filter === 'all') return true;
        return s !== 'imported' && s !== 'skipped';
      });
    },
    get importableCandidates() {
      return this.visibleCandidates.filter(c => this.statusOf(c) !== 'imported' && this.statusOf(c) !== 'skipped' && c.form.title && !c.busy);
    },
    async load(fresh = false) {
      this.loading = true; this.error = ''; this.stale = 0;
      const lib = this.lib;
      this.template = lib ? lib.settings.namingTemplate : '';
      this.provider = lib ? lib.settings.metadataProvider : 'audible';
      this.mode = lib && lib.settings.importMode === 'copy' ? 'copy' : 'move';
      try {
        const d = await api('GET', `/api/abstream/libraries/${this.libId}/import${fresh ? '?fresh=1' : ''}`);
        this.importFolder = d.importFolder; this.error = d.error || '';
        this.candidates = d.candidates.map(c => {
          const s = c.autoImport && c.autoImport.status === 'review' ? c.autoImport.suggestion : null;
          const form = this.formFrom(c.metadata, s);
          const filledAuthor = !this.authorText(c.metadata.authors) && form.authors;
          return { ...c, coverUrl: filledAuthor && s && s.cover ? s.cover : '', busy: false, error: '', form };
        });
      } catch (e) { this.error = e.message; }
      this.loading = false;
    },
    target(c) {
      if (this.lib && this.lib.settings.organizeOnImport === false) return c.relPath;
      const f = c.form;
      const authors = splitNames(f.authors);
      return renderTemplate(this.template || '{author}/{title}', {
        author: authors.length ? authors.slice(0, 3).join(' & ') : 'Unknown Author', series: f.series, sequence: f.series ? f.sequence : '',
        title: f.title, subtitle: f.subtitle, year: f.publishedYear, narrator: f.narrators, asin: f.asin,
      });
    },
    authorText(names) {
      const skip = /^(unknown( artist| author)?|various( artists)?|none|n\/a|na|soundtrack)$/i;
      return (names || []).filter(n => n && !skip.test(String(n).trim())).join(' & ');
    },
    formFrom(metadata, suggestion) {
      const s = suggestion || {};
      const series = (s.series || [])[0] || {};
      const md = metadata || {};
      return {
        title: md.title || s.title || '', subtitle: md.subtitle || s.subtitle || '',
        authors: this.authorText(md.authors) || (s.author ? splitNames(s.author).join(' & ') : ''),
        narrators: (md.narrators || []).join(', ') || s.narrator || '',
        series: md.series || series.series || '', sequence: md.sequence || (series.sequence || ''),
        publishedYear: md.publishedYear || s.publishedYear || '', asin: md.asin || s.asin || '', isbn: md.isbn || s.isbn || '',
        description: md.description || s.description || '', publisher: md.publisher || s.publisher || '',
        genres: (md.genres || []).join(', ') || (s.genres || []).join(', '), language: md.language || s.language || '',
      };
    },
    suggestionLabel(c) {
      const s = (c.autoImport && c.autoImport.suggestion) || {};
      return [s.title, s.author].filter(Boolean).join(' — ') || 'provider result';
    },
    applyResult(c, r) {
      const f = c.form;
      const s = (r.series || [])[0];
      Object.assign(f, {
        title: r.title || f.title, subtitle: r.subtitle || '', authors: r.author ? splitNames(r.author).join(' & ') : f.authors,
        narrators: r.narrator || f.narrators, series: s ? s.series : f.series, sequence: s ? (s.sequence || '') : f.sequence,
        publishedYear: r.publishedYear || f.publishedYear, asin: r.asin || f.asin, isbn: r.isbn || f.isbn,
        description: r.description || f.description, publisher: r.publisher || f.publisher,
        genres: (r.genres || []).join(', ') || f.genres, language: r.language || f.language,
      });
      c.coverUrl = r.cover || '';
    },
    useSuggestion(c) { if (c.autoImport && c.autoImport.suggestion) this.applyResult(c, c.autoImport.suggestion); },
    metadataOf(c) {
      const f = c.form;
      return {
        title: f.title, subtitle: f.subtitle || null, authors: splitNames(f.authors), narrators: splitList(f.narrators),
        series: f.series.trim() || null, sequence: f.sequence || null, publishedYear: f.publishedYear || null, asin: f.asin || null,
        isbn: f.isbn || null, description: f.description || null, publisher: f.publisher || null, genres: splitList(f.genres),
        language: f.language || null,
      };
    },
    applyBatchResult(result) {
      const imported = new Set((result.imported || []).map(x => x.relPath));
      const skipped = new Map((result.skipped || []).map(x => [x.relPath, x]));
      const failed = new Map((result.failed || []).map(x => [x.relPath, x.error]));
      this.candidates = this.candidates.filter(c => !imported.has(c.relPath));
      for (const c of this.candidates) {
        c.busy = false;
        if (skipped.has(c.relPath)) {
          const s = skipped.get(c.relPath);
          c.autoImport = { status: 'skipped', to: s.to, reason: s.reason };
          c.error = '';
        } else if (failed.has(c.relPath)) {
          c.error = failed.get(c.relPath);
        }
      }
      this.batching = false;
      this.stale = 0;
    },
    async importAll() {
      const items = this.importableCandidates;
      if (!items.length || this.batching) return;
      this.batching = true;
      for (const c of items) { c.busy = true; c.error = ''; }
      try {
        await api('POST', `/api/abstream/libraries/${this.libId}/import/batch`, {
          mode: this.mode,
          items: items.map(c => ({ relPath: c.relPath, signature: c.signature, coverUrl: c.coverUrl || null, metadata: this.metadataOf(c) })),
        });
        globalThis.Alpine.store('app').toast(`Importing ${items.length} book(s)`);
      } catch (e) {
        this.batching = false;
        for (const c of items) c.busy = false;
        globalThis.Alpine.store('app').toast(e.message, 'error');
      }
    },
    openSearch(c) { this.active = c; this.results = []; this.search(); },
    async runAutoImport() {
      try {
        await api('POST', `/api/abstream/libraries/${this.libId}/auto-import`);
        globalThis.Alpine.store('app').toast('Auto-import started');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async search() {
      const c = this.active;
      this.searching = true;
      try {
        const p = new URLSearchParams({ title: c.form.title, provider: this.provider });
        const a = splitNames(c.form.authors)[0];
        if (a) p.set('author', a);
        if (c.form.asin) p.set('asin', c.form.asin);
        this.results = await api('GET', `/api/search/books?${p}`);
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.searching = false;
    },
    pick(r) {
      this.applyResult(this.active, r);
      this.active = null;
    },
    async doImport(c) {
      const app = globalThis.Alpine.store('app');
      c.busy = true;
      try {
        const r = await api('POST', `/api/abstream/libraries/${this.libId}/import`, {
          relPath: c.relPath, mode: this.mode, coverUrl: c.coverUrl || null, metadata: this.metadataOf(c),
        });
        app.toast(`Imported to ${r.relPath}`, 'success');
        this.candidates = this.candidates.filter(x => x !== c);
      } catch (e) { app.toast(e.message, 'error'); }
      c.busy = false;
    },
  };
}

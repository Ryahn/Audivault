import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

/* Edit modal: details, match, cover, chapters, files */
export function editModal() {
  return {
    tab: 'details', item: null, form: {}, saving: false,
    providers: [], provider: 'audible', qTitle: '', qAuthor: '', results: [], searching: false, picked: null, override: true,
    covers: [], coverUrl: '', coverProvider: 'best', coverSearching: false,
    chapters: [], chapterAsin: '', files: [],
    init() {
      const m = globalThis.Alpine.store('app').modal;
      this.item = m.item;
      this.tab = m.tab || 'details';
      const md = this.item.media.metadata;
      this.form = {
        title: md.title || '', subtitle: md.subtitle || '', authors: (md.authors || []).map(a => a.name).join(' & '),
        narrators: (md.narrators || []).join(', '), series: (md.series || []).map(s => ({ name: s.name, sequence: s.sequence || '' })),
        genres: (md.genres || []).join(', '), tags: (this.item.media.tags || []).join(', '), publishedYear: md.publishedYear || '',
        publisher: md.publisher || '', language: md.language || '', isbn: md.isbn || '', asin: md.asin || '',
        description: md.description || '', explicit: !!md.explicit, abridged: !!md.abridged,
      };
      this.qTitle = md.title; this.qAuthor = md.authorName || '';
      this.chapters = (this.item.media.chapters || []).map(c => ({ ...c, startStr: fmt.clock(c.start) }));
      this.chapterAsin = md.asin || '';
      this.files = (this.item.media.audioFiles || []).map(f => ({ ...f }));
      const lib = globalThis.Alpine.store('app').libraries.find(l => l.id === this.item.libraryId);
      this.provider = (lib && lib.settings.metadataProvider) || 'audible';
      api('GET', '/api/search/providers').then(d => { this.providers = d.providers.books; this.coverProviders = d.providers.booksCovers; });
    },
    coverProviders: [],
    close() { globalThis.Alpine.store('app').modal = null; },
    async done(msg) {
      const m = globalThis.Alpine.store('app').modal;
      globalThis.Alpine.store('app').toast(msg || 'Saved', 'success');
      if (m && m.onSave) await m.onSave();
    },
    async saveDetails() {
      const f = this.form;
      this.saving = true;
      try {
        const r = await api('PATCH', `/api/items/${this.item.id}/media`, {
          metadata: {
            title: f.title, subtitle: f.subtitle || null, authors: splitNames(f.authors).map(name => ({ name })),
            narrators: splitList(f.narrators), series: f.series.filter(s => s.name.trim()).map(s => ({ name: s.name.trim(), sequence: String(s.sequence).trim() || null })),
            genres: splitList(f.genres), publishedYear: f.publishedYear || null, publisher: f.publisher || null, language: f.language || null,
            isbn: f.isbn || null, asin: f.asin || null, description: f.description || null, explicit: f.explicit, abridged: f.abridged,
          },
          tags: splitList(f.tags),
        });
        this.item = r.libraryItem;
        await this.done(r.updated ? 'Details saved' : 'No changes');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.saving = false;
    },
    async search() {
      this.searching = true; this.results = []; this.picked = null;
      try {
        const p = new URLSearchParams({ title: this.qTitle, provider: this.provider });
        if (this.qAuthor) p.set('author', this.qAuthor);
        this.results = await api('GET', `/api/search/books?${p}`);
        if (!this.results.length) globalThis.Alpine.store('app').toast('No results', 'warning');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.searching = false;
    },
    async applyMatch() {
      this.saving = true;
      try {
        const r = await api('POST', `/api/items/${this.item.id}/match`, { result: this.picked, overrideDefaults: this.override, updateCover: true });
        this.item = r.libraryItem;
        await this.done(r.updated ? 'Match applied' : 'Nothing changed');
        this.close();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.saving = false;
    },
    async searchCovers() {
      this.coverSearching = true; this.covers = [];
      try {
        const p = new URLSearchParams({ title: this.qTitle, provider: this.coverProvider });
        if (this.qAuthor) p.set('author', this.qAuthor);
        this.covers = (await api('GET', `/api/search/covers?${p}`)).results;
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.coverSearching = false;
    },
    async setCover(url) {
      try { await api('POST', `/api/items/${this.item.id}/cover`, { url }); await this.done('Cover updated'); this.close(); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async uploadCover(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      const fd = new FormData(); fd.append('cover', file);
      try { await api('POST', `/api/items/${this.item.id}/cover`, fd); await this.done('Cover uploaded'); this.close(); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async removeCover() {
      await api('DELETE', `/api/items/${this.item.id}/cover`); await this.done('Cover removed'); this.close();
    },
    addChapter() {
      const last = this.chapters[this.chapters.length - 1];
      const start = last ? Math.min(last.start + 600, this.item.media.duration) : 0;
      this.chapters.push({ start, startStr: fmt.clock(start), title: `Chapter ${this.chapters.length + 1}` });
    },
    async lookupChapters() {
      try {
        const d = await api('GET', `/api/search/chapters?asin=${encodeURIComponent(this.chapterAsin)}`);
        if (d.error) throw new Error(d.error);
        this.chapters = d.chapters.map(c => ({ start: c.startOffsetSec, startStr: fmt.clock(c.startOffsetSec), title: c.title }));
        const diff = Math.abs(d.runtimeLengthSec - this.item.media.duration);
        globalThis.Alpine.store('app').toast(diff > 60 ? `Loaded ${d.chapters.length} chapters. Runtime differs by ${fmt.duration(diff)}; check timings.` : `Loaded ${d.chapters.length} chapters`, diff > 60 ? 'warning' : 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async saveChapters() {
      const dur = this.item.media.duration;
      const list = this.chapters.map(c => ({ start: fmt.parseClock(c.startStr), title: c.title }));
      if (list.some(c => c.start === null)) return globalThis.Alpine.store('app').toast('Invalid start time', 'error');
      list.sort((a, b) => a.start - b.start);
      const chapters = list.map((c, i) => ({ id: i, start: c.start, end: i + 1 < list.length ? list[i + 1].start : dur, title: c.title }));
      try { await api('POST', `/api/items/${this.item.id}/chapters`, { chapters }); await this.done('Chapters saved'); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async saveFiles() {
      try {
        await api('POST', `/api/abstream/items/${this.item.id}/audio-files`, { exclude: this.files.filter(f => f.exclude).map(f => f.ino) });
        await this.done('Track list saved');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
  };
}

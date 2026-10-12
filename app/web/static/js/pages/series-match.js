import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function seriesMatchModal() {
  return {
    seriesId: '', seriesName: '', books: [], index: 0, provider: 'audible', providers: [],
    qTitle: '', qAuthor: '', results: [], searching: false, saving: false, loading: true,
    searchGen: 0, onDone: null,
    async init() {
      const modal = globalThis.Alpine.store('app').modal;
      this.seriesId = modal.seriesId;
      this.onDone = modal.onDone;
      try {
        const [queue, providers] = await Promise.all([
          api('GET', `/api/series/${this.seriesId}/match-queue`),
          api('GET', '/api/search/providers'),
        ]);
        this.books = (queue.books || []).map(book => ({ ...book, picked: null, results: null }));
        this.seriesName = queue.seriesName || '';
        this.provider = queue.provider || 'audible';
        this.providers = providers.providers.books;
        if (this.books.length) this.openPage();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
      this.loading = false;
    },
    get book() { return this.books[this.index] || null; },
    get atEnd() { return this.books.length > 0 && this.index >= this.books.length - 1; },
    close() { globalThis.Alpine.store('app').modal = null; },
    sourceLabel(book) {
      if (book.source === 'import') return 'Import folder';
      if (book.source === 'split') return book.sourceName || 'Another series';
      return 'This series';
    },
    numberLabel(book) {
      const current = book.sequence ? '#' + book.sequence : 'No number';
      if (book.fileSequence && book.fileSequence !== book.sequence) return current + ' · file says #' + book.fileSequence;
      return current;
    },
    seriesChip(result) {
      return (result.series || []).map(s => s.series + (s.sequence ? ' #' + s.sequence : '')).join(', ');
    },
    isSelected(result) {
      const picked = this.book && this.book.picked;
      if (!picked) return false;
      if (picked.asin && result.asin) return picked.asin === result.asin;
      return picked.title === result.title && (picked.author || '') === (result.author || '');
    },
    select(result) {
      if (!this.book) return;
      this.book.picked = result;
    },
    stashQuery() {
      if (!this.book) return;
      this.book.queryTitle = this.qTitle;
      this.book.queryAuthor = this.qAuthor;
    },
    openPage() {
      const book = this.book;
      if (!book) return;
      this.qTitle = book.queryTitle != null ? book.queryTitle : (book.title || '');
      this.qAuthor = book.queryAuthor != null ? book.queryAuthor : (book.author || '');
      this.results = book.results || [];
      if (!book.results) this.search(false);
    },
    async search(force) {
      const book = this.book;
      if (!book) return;
      this.stashQuery();
      if (!force && book.results) {
        this.results = book.results;
        return;
      }
      const gen = ++this.searchGen;
      this.searching = true;
      this.results = [];
      try {
        const seen = new Set();
        const results = [];
        const add = (rows) => {
          for (const row of rows || []) {
            const id = (row.asin || '') + '|' + (row.title || '') + '|' + (row.author || '');
            if (seen.has(id)) continue;
            seen.add(id);
            results.push(row);
          }
        };
        for (const asin of book.asins || []) {
          const params = new URLSearchParams({ title: asin, provider: this.provider, asin });
          add(await api('GET', `/api/search/books?${params}`));
          if (gen !== this.searchGen) return;
        }
        if ((this.qTitle || '').trim()) {
          const params = new URLSearchParams({ title: this.qTitle.trim(), provider: this.provider });
          if (this.qAuthor) params.set('author', this.qAuthor);
          add(await api('GET', `/api/search/books?${params}`));
        }
        if (gen !== this.searchGen) return;
        book.results = results;
        this.results = results;
      } catch (e) {
        if (gen === this.searchGen) globalThis.Alpine.store('app').toast(e.message, 'error');
      } finally {
        if (gen === this.searchGen) this.searching = false;
      }
    },
    skip() {
      if (!this.book) return;
      this.book.picked = null;
      if (!this.atEnd) this.forward();
    },
    back() {
      if (this.index === 0) return;
      this.stashQuery();
      this.index -= 1;
      this.openPage();
    },
    forward() {
      this.stashQuery();
      this.index += 1;
      this.openPage();
    },
    next() {
      if (this.atEnd) return this.save();
      this.forward();
    },
    async save() {
      const app = globalThis.Alpine.store('app');
      const picks = this.books.filter(book => book.picked);
      if (!picks.length) {
        app.toast('No matches selected', 'warning');
        return;
      }
      this.saving = true;
      try {
        const response = await api('POST', `/api/series/${this.seriesId}/match`, {
          picks: picks.map(book => ({ kind: book.kind, itemId: book.itemId, relPath: book.relPath, result: book.picked })),
        });
        const done = await app.waitForTask(response.task);
        if (done.status === 'failed') return;
        const finish = this.onDone;
        this.close();
        if (finish) await finish();
      } catch (e) { app.toast(e.message, 'error'); }
      finally { this.saving = false; }
    },
  };
}

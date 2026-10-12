import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

const SORTS = [
  ['media.metadata.title', 'Title'], ['media.metadata.authorName', 'Author'], ['media.metadata.authorNameLF', 'Author (Last, First)'],
  ['media.metadata.publishedYear', 'Published year'], ['addedAt', 'Date added'], ['media.duration', 'Duration'], ['size', 'Size'],
  ['progress', 'Progress updated'], ['progress.percent', 'Percent complete'], ['progress.isFinished', 'Finished'],
  ['birthtimeMs', 'File birthtime'], ['mtimeMs', 'File modified'], ['random', 'Random'],
];
const SERIES_SORTS = [
  ['name', 'Name'], ['numBooks', 'Number of books'], ['finished', 'Finished books'],
  ['completion', 'Percent complete'], ['totalDuration', 'Duration'], ['lastBookAdded', 'Recently added'],
];
const DESC_SORTS = ['addedAt', 'progress', 'progress.percent', 'progress.isFinished', 'media.duration', 'size', 'birthtimeMs', 'mtimeMs'];
const SERIES_DESC_SORTS = ['numBooks', 'finished', 'completion', 'totalDuration', 'lastBookAdded'];

export function libraryView() {
  return {
    tab: 'books', items: [], total: 0, page: 0, limit: 60, loading: false, done: false,
    sort: localStorage.getItem('abstream_sort') || 'media.metadata.title', desc: localStorage.getItem('abstream_desc') === '1',
    seriesSort: localStorage.getItem('abstream_series_sort') || 'name', seriesDesc: localStorage.getItem('abstream_series_desc') === '1',
    collapse: localStorage.getItem('abstream_collapse') === '1', filter: '', filterLabel: '', q: '',
    filterData: null, filterLoading: false, menu: null, series: [], authors: [], collections: [], sorts: SORTS, seriesSorts: SERIES_SORTS,
    selected: [], selectMode: false,
    destroy() { this.$cleanup && this.$cleanup(); },

    async init() {
      onLibraryChange(this, () => this.refreshQuiet());
      const app = globalThis.Alpine.store('app');
      const libId = app.route.params[0];
      if (libId && libId !== app.libraryId) app.setLibrary(libId);
      this.tab = app.route.query.tab || 'books';
      this.q = app.route.query.q || '';
      if (app.route.query.filter) { this.filter = app.route.query.filter; this.filterLabel = app.route.query.label || 'Filter'; }
      this.reload();
      const sentinel = this.$refs.sentinel;
      if (sentinel) new IntersectionObserver(es => { if (es[0].isIntersecting) this.more(); }, { rootMargin: '600px' }).observe(sentinel);
    },
    clearGrid(ref) {
      const el = this.$refs[ref];
      if (el) el.innerHTML = '';
    },
    appendCards(ref, html) {
      const el = this.$refs[ref];
      if (el && html) el.insertAdjacentHTML('beforeend', html);
    },
    setTab(t) { this.tab = t; this.reload(); history.replaceState(null, '', `#/library/${globalThis.Alpine.store('app').libraryId}?tab=${t}`); },
    reload() {
      this.items = []; this.series = []; this.authors = []; this.collections = [];
      this.page = 0; this.done = false; this.selected = [];
      this.$nextTick(() => {
        this.clearGrid('booksGrid'); this.clearGrid('seriesGrid'); this.clearGrid('authorsGrid');
        this.more();
      });
    },
    async ensureFilterData() {
      if (this.filterData || this.filterLoading) return;
      this.filterLoading = true;
      try { this.filterData = await api('GET', `/api/libraries/${globalThis.Alpine.store('app').libraryId}/filterdata`); }
      catch (_) { /* ignore */ }
      this.filterLoading = false;
    },
    async toggleFilterMenu() {
      this.menu = this.menu === 'filter' ? null : 'filter';
      if (this.menu === 'filter') await this.ensureFilterData();
    },
    async more() {
      if (this.loading || this.done) return;
      const app = globalThis.Alpine.store('app');
      const lib = app.libraryId;
      this.loading = true;
      try {
        if (this.tab === 'books') {
          const params = new URLSearchParams({ limit: this.limit, page: this.page, sort: this.sort, desc: this.desc ? 1 : 0, minified: 1 });
          if (this.filter) params.set('filter', this.filter);
          if (this.collapse && !this.filter && !this.q) params.set('collapseseries', 1);
          if (this.q) params.set('q', this.q);
          const data = await api('GET', `/api/libraries/${lib}/items?${params}`);
          const batch = data.results;
          this.items.push(...batch);
          this.total = data.total;
          this.page++;
          this.done = this.items.length >= data.total;
          if (!this.selectMode) this.appendCards('booksGrid', batch.map(i => bookCard(i)).join(''));
        } else if (this.tab === 'series') {
          const data = await api('GET', `/api/libraries/${lib}/series?limit=60&page=${this.page}&sort=${encodeURIComponent(this.seriesSort)}&desc=${this.seriesDesc ? 1 : 0}&minified=1`);
          const batch = data.results;
          this.series = this.page === 0 ? batch : this.series.concat(batch);
          this.total = data.total; this.page++; this.done = this.series.length >= data.total;
          this.appendCards('seriesGrid', batch.map(s => seriesCard(s)).join(''));
        } else if (this.tab === 'authors') {
          const data = await api('GET', `/api/libraries/${lib}/authors?sort=name&limit=${this.limit}&page=${this.page}`);
          const batch = data.authors || data.results || [];
          this.authors = this.page === 0 ? batch : this.authors.concat(batch);
          this.total = data.total; this.page++; this.done = this.authors.length >= data.total;
          this.appendCards('authorsGrid', batch.map(a => authorCard(a)).join(''));
        } else if (this.tab === 'collections') {
          const data = await api('GET', `/api/libraries/${lib}/collections?minified=1`);
          this.collections = data.results; this.total = data.total; this.done = true;
        }
      } catch (e) { app.toast(e.message, 'error'); this.done = true; }
      this.loading = false;
    },
    setSort(s) {
      if (this.sort === s) this.desc = !this.desc; else { this.sort = s; this.desc = DESC_SORTS.includes(s); }
      localStorage.setItem('abstream_sort', this.sort); localStorage.setItem('abstream_desc', this.desc ? '1' : '0');
      this.menu = null; this.reload();
    },
    setSeriesSort(s) {
      if (this.seriesSort === s) this.seriesDesc = !this.seriesDesc; else { this.seriesSort = s; this.seriesDesc = SERIES_DESC_SORTS.includes(s); }
      localStorage.setItem('abstream_series_sort', this.seriesSort); localStorage.setItem('abstream_series_desc', this.seriesDesc ? '1' : '0');
      this.menu = null; this.reload();
    },
    async refreshQuiet() {
      if (this.selectMode || this.loading) return;
      if (this.tab !== 'collections' && this.page !== 1) return;
      const app = globalThis.Alpine.store('app');
      const lib = app.libraryId;
      const tab = this.tab;
      try {
        if (tab === 'books') {
          const params = new URLSearchParams({ limit: this.limit, page: 0, sort: this.sort, desc: this.desc ? 1 : 0, minified: 1 });
          if (this.filter) params.set('filter', this.filter);
          if (this.collapse && !this.filter && !this.q) params.set('collapseseries', 1);
          if (this.q) params.set('q', this.q);
          const data = await api('GET', `/api/libraries/${lib}/items?${params}`);
          if (this.selectMode || this.page !== 1 || this.tab !== 'books') return;
          this.items = data.results;
          this.total = data.total;
          this.done = this.items.length >= data.total;
          const el = this.$refs.booksGrid;
          if (el) el.innerHTML = data.results.map(i => bookCard(i)).join('');
        } else if (tab === 'series') {
          const data = await api('GET', `/api/libraries/${lib}/series?limit=60&page=0&sort=${encodeURIComponent(this.seriesSort)}&desc=${this.seriesDesc ? 1 : 0}&minified=1`);
          if (this.page !== 1 || this.tab !== 'series') return;
          this.series = data.results;
          this.total = data.total;
          this.done = this.series.length >= data.total;
          const el = this.$refs.seriesGrid;
          if (el) el.innerHTML = data.results.map(s => seriesCard(s)).join('');
        } else if (tab === 'authors') {
          const data = await api('GET', `/api/libraries/${lib}/authors?sort=name&limit=${this.limit}&page=0`);
          if (this.page !== 1 || this.tab !== 'authors') return;
          const batch = data.authors || data.results || [];
          this.authors = batch;
          this.total = data.total;
          this.done = this.authors.length >= data.total;
          const el = this.$refs.authorsGrid;
          if (el) el.innerHTML = batch.map(a => authorCard(a)).join('');
        } else if (tab === 'collections') {
          const data = await api('GET', `/api/libraries/${lib}/collections?minified=1`);
          if (this.tab !== 'collections') return;
          this.collections = data.results;
          this.total = data.total;
        }
      } catch (e) { /* keep the grid that is already on screen */ }
    },
    toggleCollapse() { this.collapse = !this.collapse; localStorage.setItem('abstream_collapse', this.collapse ? '1' : '0'); this.reload(); },
    setFilter(group, value, label) {
      this.filter = group ? `${group}.${encodeURIComponent(fmt.b64(value))}` : '';
      this.filterLabel = label || '';
      this.menu = null; this.reload();
    },
    clearSearch() { this.q = ''; globalThis.Alpine.store('app').search = ''; this.reload(); },
    toggleSelect(id) { this.selected = this.selected.includes(id) ? this.selected.filter(x => x !== id) : [...this.selected, id]; },
    async quickMatchSelected() {
      const app = globalThis.Alpine.store('app');
      const res = await api('POST', '/api/items/batch/quickmatch', { libraryItemIds: this.selected, options: { overrideDefaults: false } });
      app.toast(`Matching ${this.selected.length} books in the background`);
      this.selectMode = false; this.selected = [];
      app.waitForTask(res.task).then(() => this.reload());
    },
    sortLabel() { return (SORTS.find(s => s[0] === this.sort) || ['', 'Title'])[1]; },
    seriesSortLabel() { return (SERIES_SORTS.find(s => s[0] === this.seriesSort) || ['', 'Name'])[1]; },
  };
}

import { api, fmt, esc } from './core.js';
import { ui } from './ui.js';

const cardModels = new Map();

function coverMenuButton(key) {
  return `<button type="button" class="cover-more" aria-label="More" aria-haspopup="menu" onclick="event.preventDefault();event.stopPropagation();openCoverMenu(this,'${key}')"><i class="bi bi-three-dots"></i></button>`;
}

export function bookCard(item, opts = {}) {
  const md = item.media.metadata;
  const app = globalThis.Alpine.store('app');
  const cs = item.collapsedSeries;
  const href = cs ? `#/series/${cs.id}` : `#/item/${item.id}`;
  const p = app.progressOf(item.id);
  const pct = p ? (p.isFinished ? 100 : Math.round((p.progress || 0) * 100)) : 0;
  const seq = opts.sequence ?? (md.series && !Array.isArray(md.series) ? md.series.sequence : null);
  const cover = app.cover(item, true);
  const title = cs ? cs.name : md.title;
  const sub = cs ? `${cs.numBooks} books` : (md.authorName || 'Unknown author');
  const hasAudio = (item.media.numTracks || 0) > 0;
  const menuKey = cs ? `collapsed:${cs.id}` : `book:${item.id}`;
  cardModels.set(menuKey, { kind: cs ? 'collapsed' : 'book', item, opts });
  return `<a class="book-card ${cs ? 'series-stack' : ''}" data-card="${menuKey}" href="${href}" title="${esc(title)}">
    <div class="cover">
      ${cover ? `<img loading="lazy" decoding="async" src="${cover}" alt="">` : `<div class="cover-ph">${esc(title)}<small>${esc(md.authorName || '')}</small></div>`}
      ${coverMenuButton(menuKey)}
      ${seq ? `<span class="badge-seq">#${esc(seq)}</span>` : ''}
      ${cs ? `<span class="badge-count">${cs.numBooks}</span>` : ''}
      ${item.isMissing ? '<span class="chip chip-danger missing-flag">Missing</span>' : ''}
      ${p && p.isFinished ? '<span class="badge-done" title="Finished"><i class="bi bi-check-lg"></i></span>' : ''}
      ${!cs && hasAudio ? `<button type="button" class="play-overlay" aria-label="Play" onclick="event.preventDefault();event.stopPropagation();globalThis.Alpine.store('player').play('${item.id}')"><i class="bi bi-play-fill"></i></button>` : ''}
      ${pct > 0 && pct < 100 ? `<div class="progress-line"><span style="width:${pct}%"></span></div>` : ''}
    </div>
    <div class="book-meta"><div class="book-title">${esc(title)}</div><div class="book-sub">${esc(sub)}</div></div>
  </a>`;
}

export function seriesCard(series) {
  const books = series.books || [];
  const num = series.numBooks != null ? series.numBooks : books.length;
  const first = books.find(b => b.media && b.media.coverPath) || books[0];
  const cover = first && globalThis.Alpine.store('app').cover(first, true);
  const menuKey = `series:${series.id}`;
  cardModels.set(menuKey, { kind: 'series', series });
  return `<a class="book-card series-stack" data-card="${menuKey}" href="#/series/${series.id}" title="${esc(series.name)}">
    <div class="cover">
      ${cover ? `<img loading="lazy" decoding="async" src="${cover}" alt="">` : `<div class="cover-ph">${esc(series.name)}</div>`}
      ${coverMenuButton(menuKey)}
      <span class="badge-count">${num}</span>
    </div>
    <div class="book-meta"><div class="book-title">${esc(series.name)}</div><div class="book-sub">${num} book${num === 1 ? '' : 's'}${series.finishedBooks != null ? ` · ${series.finishedBooks} finished` : ''} · ${fmt.duration(series.totalDuration)}</div></div>
  </a>`;
}

export function authorCard(a) {
  const initials = a.name.split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase();
  return `<a class="author-card book-card" href="#/author/${a.id}" title="${esc(a.name)}">
    <div class="avatar">${a.imagePath ? `<img loading="lazy" decoding="async" src="/api/authors/${a.id}/image?ts=${a.updatedAt}" alt="">` : esc(initials)}</div>
    <div class="book-meta"><div class="book-title">${esc(a.name)}</div><div class="book-sub">${a.numBooks || 0} book${a.numBooks === 1 ? '' : 's'}</div></div>
  </a>`;
}

function paintCards(key) {
  const model = cardModels.get(key);
  if (!model) return;
  const nodes = document.querySelectorAll(`[data-card="${CSS.escape(key)}"]`);
  if (!nodes.length) return;
  const html = model.kind === 'series' ? seriesCard(model.series) : bookCard(model.item, model.opts || {});
  nodes.forEach(el => {
    el.insertAdjacentHTML('afterend', html);
    el.remove();
  });
}

async function markBooksFinished(ids, finished) {
  const app = globalThis.Alpine.store('app');
  if (!ids.length) return;
  if (ids.length === 1) {
    const p = await api('PATCH', `/api/me/progress/${ids[0]}`, { isFinished: finished });
    app.progressMap = { ...app.progressMap, [ids[0]]: p };
    return;
  }
  await api('PATCH', '/api/me/progress/batch/update', ids.map(libraryItemId => ({ libraryItemId, isFinished: finished })));
  await app.refreshUser();
}

function seriesIsFinished(model) {
  const app = globalThis.Alpine.store('app');
  if (model.kind === 'collapsed') {
    const ids = model.item.collapsedSeries.libraryItemIds || [];
    return ids.length > 0 && ids.every(id => (app.progressOf(id) || {}).isFinished);
  }
  const series = model.series;
  if (series.finishedBooks != null && series.numBooks != null)
    return series.numBooks > 0 && series.finishedBooks >= series.numBooks;
  const books = series.books || [];
  const num = series.numBooks != null ? series.numBooks : books.length;
  if (!books.length || books.length < num) return false;
  return books.every(b => (app.progressOf(b.id) || {}).isFinished);
}

async function seriesBookIds(model) {
  if (model.kind === 'collapsed') {
    const ids = model.item.collapsedSeries.libraryItemIds || [];
    if (ids.length) return ids;
  } else {
    const books = model.series.books || [];
    const num = model.series.numBooks != null ? model.series.numBooks : books.length;
    if (books.length && books.length >= num) return books.map(b => b.id);
  }
  const id = model.kind === 'series' ? model.series.id : model.item.collapsedSeries.id;
  const full = await api('GET', `/api/series/${id}`);
  return (full.books || []).map(b => b.id);
}

async function seriesBooks(model) {
  if (model.kind === 'series') {
    const books = model.series.books || [];
    const num = model.series.numBooks != null ? model.series.numBooks : books.length;
    if (books.length && books.length >= num) return books;
  }
  const id = model.kind === 'series' ? model.series.id : model.item.collapsedSeries.id;
  const full = await api('GET', `/api/series/${id}`);
  return full.books || [];
}

function coverMenuHtml(model) {
  const app = globalThis.Alpine.store('app');
  if (model.kind === 'book') {
    const finished = !!(app.progressOf(model.item.id) || {}).isFinished;
    let html = `<button type="button" data-action="finish"><i class="bi ${finished ? 'bi-arrow-counterclockwise' : 'bi-check2-circle'}"></i> ${finished ? 'Mark unfinished' : 'Mark finished'}</button>`;
    if (app.canUpdate) {
      html += `<button type="button" data-action="edit"><i class="bi bi-pencil"></i> Edit</button>`;
      html += `<button type="button" data-action="match"><i class="bi bi-magic"></i> Quick match</button>`;
    }
    html += `<button type="button" data-action="collection"><i class="bi bi-bookmark-plus"></i> Add to collection</button>`;
    return html;
  }
  const finished = seriesIsFinished(model);
  let html = `<button type="button" data-action="finish"><i class="bi ${finished ? 'bi-arrow-counterclockwise' : 'bi-check2-circle'}"></i> ${finished ? 'Mark series unfinished' : 'Mark series finished'}</button>`;
  if (app.canUpdate) html += `<button type="button" data-action="rename"><i class="bi bi-pencil"></i> Rename</button>`;
  html += `<button type="button" data-action="play"><i class="bi bi-play-fill"></i> Play next unfinished</button>`;
  return html;
}

function ensureCoverMenu() {
  let menu = document.getElementById('cover-menu');
  if (menu) return menu;
  menu = document.createElement('div');
  menu.id = 'cover-menu';
  menu.className = 'menu cover-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  menu.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const key = menu.dataset.key;
    const action = button.dataset.action;
    closeCoverMenu();
    runCoverAction(key, action);
  });
  document.body.appendChild(menu);
  document.addEventListener('click', event => {
    if (menu.hidden) return;
    if (menu.contains(event.target)) return;
    if (event.target.closest && event.target.closest('.cover-more')) return;
    closeCoverMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeCoverMenu();
    }
  }, true);
  window.addEventListener('scroll', () => { if (!menu.hidden) closeCoverMenu(); }, true);
  window.addEventListener('hashchange', closeCoverMenu);
  return menu;
}

function positionCoverMenu(menu, anchor) {
  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(8, rect.left)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  const box = menu.getBoundingClientRect();
  if (box.bottom > window.innerHeight - 8)
    menu.style.top = `${Math.max(8, rect.top - box.height - 6)}px`;
  const placed = menu.getBoundingClientRect();
  if (placed.right > window.innerWidth - 8)
    menu.style.left = `${Math.max(8, window.innerWidth - placed.width - 8)}px`;
}

function closeCoverMenu() {
  const menu = document.getElementById('cover-menu');
  if (!menu) return;
  menu.hidden = true;
  menu.dataset.key = '';
}

export function openCoverMenu(anchor, key) {
  const model = cardModels.get(key);
  if (!model) return;
  const menu = ensureCoverMenu();
  if (!menu.hidden && menu.dataset.key === key) {
    closeCoverMenu();
    return;
  }
  menu.innerHTML = coverMenuHtml(model);
  menu.dataset.key = key;
  menu.hidden = false;
  positionCoverMenu(menu, anchor);
}

async function runCoverAction(key, action) {
  const model = cardModels.get(key);
  if (!model) return;
  try {
    if (model.kind === 'book') await runBookCoverAction(model, action);
    else await runSeriesCoverAction(model, action);
  } catch (e) {
    globalThis.Alpine.store('app').toast(e.message, 'error');
  }
}

async function runBookCoverAction(model, action) {
  const app = globalThis.Alpine.store('app');
  const item = model.item;
  if (action === 'finish') {
    const finished = !!(app.progressOf(item.id) || {}).isFinished;
    await markBooksFinished([item.id], !finished);
    paintCards(`book:${item.id}`);
    app.toast(!finished ? 'Marked as finished' : 'Marked as not finished', 'success');
  } else if (action === 'edit') {
    const full = await api('GET', `/api/items/${item.id}?expanded=1`);
    app.modal = {
      type: 'edit', item: full, tab: 'details',
      onSave: async () => {
        try {
          const fresh = await api('GET', `/api/items/${item.id}?expanded=1`);
          const current = cardModels.get(`book:${item.id}`);
          if (current) current.item = fresh;
          paintCards(`book:${item.id}`);
        } catch (e) { app.toast(e.message, 'error'); }
      },
    };
  } else if (action === 'match') {
    app.toast('Searching providers…');
    const result = await api('POST', `/api/items/${item.id}/match`, { overrideDefaults: false });
    app.toast(result.updated ? 'Book details updated' : (result.warning || 'No changes'), result.updated ? 'success' : 'warning');
    if (result.libraryItem) {
      const current = cardModels.get(`book:${item.id}`);
      if (current) current.item = result.libraryItem;
      paintCards(`book:${item.id}`);
    }
  } else if (action === 'collection') {
    app.modal = { type: 'collection', item };
  }
}

async function runSeriesCoverAction(model, action) {
  const app = globalThis.Alpine.store('app');
  const seriesId = model.kind === 'series' ? model.series.id : model.item.collapsedSeries.id;
  if (action === 'finish') {
    const finished = seriesIsFinished(model);
    const ids = await seriesBookIds(model);
    await markBooksFinished(ids, !finished);
    if (model.kind === 'series') {
      const num = model.series.numBooks != null ? model.series.numBooks : ids.length;
      model.series.finishedBooks = !finished ? num : 0;
    }
    paintCards(`series:${seriesId}`);
    paintCards(`collapsed:${seriesId}`);
    ids.forEach(id => paintCards(`book:${id}`));
    app.toast(!finished ? 'Series marked as finished' : 'Series marked as not finished', 'success');
  } else if (action === 'rename') {
    const current = model.kind === 'series' ? model.series.name : model.item.collapsedSeries.name;
    const name = await ui.prompt({ title: 'Rename series', label: 'Name', value: current, confirmText: 'Save' });
    if (!name || name === current) return;
    const updated = await api('PATCH', `/api/series/${seriesId}`, { name });
    const next = (updated && updated.name) || name;
    const seriesModel = cardModels.get(`series:${seriesId}`);
    if (seriesModel) seriesModel.series.name = next;
    const collapsedModel = cardModels.get(`collapsed:${seriesId}`);
    if (collapsedModel) collapsedModel.item.collapsedSeries.name = next;
    paintCards(`series:${seriesId}`);
    paintCards(`collapsed:${seriesId}`);
    app.toast('Series renamed', 'success');
  } else if (action === 'play') {
    const books = await seriesBooks(model);
    const next = books.find(b => !(app.progressOf(b.id) || {}).isFinished) || books[0];
    if (next) globalThis.Alpine.store('player').play(next.id);
  }
}


import { api, fmt, registerApp } from './core.js';
import { authorCard, bookCard, openCoverMenu, seriesCard } from './cards.js';
import { bindPlayerKeys, registerPlayer } from './player.js';
import { bindDismiss, ui } from './ui.js';
import { authView } from './pages/auth.js';
import { authorView } from './pages/author.js';
import { duplicatesView } from './pages/duplicates.js';
import { editModal } from './pages/edit.js';
import { homeView } from './pages/home.js';
import { importView } from './pages/import.js';
import { itemView } from './pages/item.js';
import { libraryView } from './pages/library.js';
import { organizeItemModal } from './pages/organize-item.js';
import { organizeView } from './pages/organize.js';
import { seriesMatchModal } from './pages/series-match.js';
import { seriesView } from './pages/series.js';
import { settingsView } from './pages/settings.js';
import { statsView } from './pages/stats.js';

const pages = {
  authView, authorView, duplicatesView, editModal, homeView, importView, itemView,
  libraryView, organizeItemModal, organizeView, seriesMatchModal, seriesView,
  settingsView, statsView,
};

const markup = { api, fmt, ui, bookCard, seriesCard, authorCard, openCoverMenu, ...pages };

const alpine = await import('https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/module.esm.js');
const Alpine = alpine.default;
globalThis.Alpine = Alpine;

registerApp(Alpine);
registerPlayer(Alpine);
bindPlayerKeys();
bindDismiss();

for (const [name, factory] of Object.entries(pages)) {
  Alpine.data(name, factory);
}

Object.assign(globalThis, markup);
Alpine.start();

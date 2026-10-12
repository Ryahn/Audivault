import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

const LIB_TOGGLES = [
  ['organizeOnImport', 'Organize on import', 'Imported books are placed using the folder template. When off, they keep the folder or file name they had in the import folder.'],
  ['organizeOnScan', 'Organize on scan', "After every scan (including automatic ones), books whose folder doesn't match the template are renamed, including a change that only fixes capitalization. Books that would land in a folder that already belongs to something else, books whose folder is really gone, leftover entries, and books with no author are skipped. Leftover entries can be removed from the Organize page."],
  ['useCueChapters', 'Use .cue files for chapters', 'When a book folder has a .cue sheet, its tracks become the chapters (over embedded chapters and metadata.json). Changing this refreshes existing books.'],
  ['watchImportFolder', 'Watch the import folder', 'Get a notification when new books land in the import folder.'],
  ['writeMetadataJson', 'Write metadata.json into book folders', 'Keeps your edits with the files (Audiobookshelf format).'],
  ['storeCoverWithItem', 'Save covers inside book folders', 'Otherwise covers are kept in the server metadata folder.'],
  ['renameAudioFiles', 'Rename audio files when organizing', 'Files become "Title - Part 01.mp3"; single files become "Title.m4b".'],
  ['parseSubtitle', 'Parse subtitle from folder name', '"Title - Subtitle" folder names are split on " - ".'],
  ['preferFolderMetadata', 'Prefer folder names over embedded tags', 'By default embedded tags override the folder structure.'],
  ['hideSingleBookSeries', 'Hide single-book series', 'In series views and collapsed series.'],
];

export function settingsView() {
  return {
    tab: 'libraries', users: [], keys: [], activity: [], editingLib: null, savingLib: false, userForm: null, newKey: null, keyForm: null,
    picker: null, preview: [], pw: { password: '', newPassword: '' }, providers: [], toggles: LIB_TOGGLES, watching: [],
    trash: { retentionDays: 30, lockedByEnv: false, entries: [] }, trashDays: 30, trashLoading: true, trashError: false,
    plugins: { catalog: {}, cache: {}, installed: [], available: [], nav: [] }, pluginValues: {}, pluginDelete: {},
    destroy() {
      window.removeEventListener('abstream-activity', this._onActivity);
      window.removeEventListener('abstream-trash-changed', this._onTrash);
    },
    async init() {
      const app = globalThis.Alpine.store('app');
      this._onActivity = e => { this.activity = [e.detail, ...this.activity].slice(0, 300); };
      window.addEventListener('abstream-activity', this._onActivity);
      this._onTrash = () => { if (this.tab === 'trash') this.loadTab(); };
      window.addEventListener('abstream-trash-changed', this._onTrash);
      this.tab = app.route.params[0] || (app.isAdmin ? 'libraries' : 'account');
      api('GET', '/api/search/providers').then(d => { this.providers = d.providers.books; }).catch(() => { });
      this.loadTab();
      this.$watch('$store.app.route.params', () => {
        if (this.tab !== 'plugins') return;
        const plugin = (this.plugins.installed || []).find(item => item.id === globalThis.Alpine.store('app').route.params[1]);
        if (plugin) this.fillPluginValues(plugin);
      });
    },
    setTab(t) { this.tab = t; history.replaceState(null, '', `#/settings/${t}`); this.loadTab(); },
    async loadTab() {
      try {
        if (this.tab === 'users') this.users = (await api('GET', '/api/users')).users;
        if (this.tab === 'keys') { this.keys = (await api('GET', '/api/api-keys')).apiKeys; if (!this.users.length) this.users = (await api('GET', '/api/users')).users; }
        if (this.tab === 'trash') {
          this.trashLoading = true;
          this.trashError = false;
          try {
            this.trash = await api('GET', '/api/abstream/trash');
            this.trashDays = this.trash.retentionDays;
          } catch (e) {
            this.trashError = true;
            throw e;
          } finally {
            this.trashLoading = false;
          }
        }
        if (this.tab === 'activity') {
          this.activity = (await api('GET', '/api/abstream/activity')).activity;
          this.watching = (await api('GET', '/api/abstream/watcher')).watching;
        }
        if (this.tab === 'plugins') {
          this.plugins = await api('GET', '/api/abstream/plugins');
          const focus = globalThis.Alpine.store('app').route.params[1];
          const plugin = (this.plugins.installed || []).find(item => item.id === focus);
          if (plugin) this.fillPluginValues(plugin);
        }
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    newLibrary() {
      this.editingLib = { id: null, name: '', folders: [], settings: { importFolder: '', namingTemplate: '{author}/[{series}/][{sequence} - ]{title}', metadataProvider: 'audible', writeMetadataJson: true, storeCoverWithItem: true, renameAudioFiles: false, parseSubtitle: false, preferFolderMetadata: false, hideSingleBookSeries: false, scanIntervalMinutes: 0, metadataRefreshHours: 0, markAsFinishedTimeRemaining: 10, locked: false, useCueChapters: false, importMode: 'move', autoImport: false, organizeOnImport: true, organizeOnScan: false, disableWatcher: false, watchMode: 'auto', watchImportFolder: true } };
      this.newFolder = '';
      this.savingLib = false;
      this.updatePreview();
    },
    editLibrary(lib) {
      this.editingLib = JSON.parse(JSON.stringify({ id: lib.id, name: lib.name, folders: lib.folders.map(f => ({ fullPath: f.fullPath, locked: !!f.locked })), settings: lib.settings }));
      this.editingLibWasOrganizing = !!lib.settings.organizeOnScan;
      this.newFolder = '';
      this.savingLib = false;
      this.updatePreview();
    },
    newFolder: '',
    addFolder() {
      const p = this.newFolder.trim();
      if (p && !this.editingLib.folders.some(f => f.fullPath === p)) this.editingLib.folders.push({ fullPath: p, locked: false });
      this.newFolder = '';
    },
    updatePreview() {
      const t = this.editingLib.settings.namingTemplate || '';
      this.preview = [
        renderTemplate(t, { author: 'Brandon Sanderson', series: 'Mistborn', sequence: '1', title: 'The Final Empire', year: '2006', narrator: 'Michael Kramer', asin: 'B002UZMLXM', subtitle: '' }),
        renderTemplate(t, { author: 'Andy Weir', series: '', sequence: '', title: 'Project Hail Mary', year: '2021', narrator: 'Ray Porter', asin: 'B08G9PRS1K', subtitle: '' }),
      ];
    },
    async saveLibrary() {
      if (this.savingLib) return;
      const app = globalThis.Alpine.store('app');
      const l = this.editingLib;
      if (!l) return;
      if (this.newFolder.trim()) this.addFolder();
      const body = { name: l.name, folders: l.folders.map(f => ({ fullPath: f.fullPath, locked: !!f.locked })), settings: { ...l.settings, scanIntervalMinutes: Number(l.settings.scanIntervalMinutes) || 0, metadataRefreshHours: Math.max(0, Number(l.settings.metadataRefreshHours) || 0) } };
      if (l.id && l.settings.organizeOnScan && !this.editingLibWasOrganizing) {
        // Turning this on renames folders on disk at the next scan: show how many first
        let count = '?';
        try {
          const p = new URLSearchParams({ template: l.settings.namingTemplate });
          count = (await api('GET', `/api/abstream/libraries/${l.id}/organize?${p}`)).plans.filter(x => x.status === 'ok').length;
        } catch (e) { }
        const ok = await ui.confirm({
          title: 'Turn on organize on scan?',
          message: `${count} book folder(s) in "${l.name}" don't match the template and will be renamed on the next scan. Books without an author are skipped. You can preview the changes on the Organize page.`,
          detail: l.settings.namingTemplate, confirmText: 'Turn on',
        });
        if (!ok) return;
      }
      this.savingLib = true;
      try {
        if (l.id) {
          await api('PATCH', `/api/libraries/${l.id}`, body);
          app.toast('Library saved', 'success');
        } else {
          const lib = await api('POST', '/api/libraries', body);
          app.setLibrary(lib.id);
          app.toast('Library created — scan started in the background', 'success');
          app.pollTasks();
        }
        this.editingLib = null;
        await app.loadLibraries();
      } catch (e) { app.toast(e.message, 'error'); }
      finally { this.savingLib = false; }
    },
    async scan(lib, force) {
      const app = globalThis.Alpine.store('app');
      await api('POST', `/api/libraries/${lib.id}/scan?force=${force ? 1 : 0}`);
      app.toast(`Scanning ${lib.name}…`);
      app.pollTasks();
    },
    async refreshMetadata(lib) {
      const app = globalThis.Alpine.store('app');
      try {
        await api('POST', `/api/libraries/${lib.id}/refresh-metadata`);
        app.toast(`Refreshing empty metadata for ${lib.name}`);
        app.pollTasks();
      } catch (e) { app.toast(e.message, 'error'); }
    },
    async removeMissing(lib) {
      if (!await ui.confirm({ title: 'Remove missing items?', message: `Books in "${lib.name}" whose folders no longer exist on disk will be removed from the database, along with their listening progress.`, confirmText: 'Remove missing', danger: true })) return;
      const r = await api('DELETE', `/api/libraries/${lib.id}/issues`);
      globalThis.Alpine.store('app').toast(`Removed ${r.removed} missing item(s)`, 'success');
    },
    async deleteLibrary(lib) {
      if (!await ui.confirm({ title: 'Delete library?', message: `"${lib.name}" and its database records will be deleted. Files on disk are not touched.`, confirmText: 'Delete library', danger: true })) return;
      const app = globalThis.Alpine.store('app');
      await api('DELETE', `/api/libraries/${lib.id}`);
      await app.loadLibraries();
      if (app.libraryId === lib.id) app.setLibrary(app.libraries[0] ? app.libraries[0].id : '');
    },
    openPicker(target) { this.picker = { target, path: '', dirs: [], parent: null }; this.browse(''); },
    async browse(path) {
      try { const d = await api('GET', `/api/abstream/fs?path=${encodeURIComponent(path)}`); Object.assign(this.picker, d); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    choose() {
      const p = this.picker.path;
      if (!p) return;
      if (this.picker.target === 'folder') { if (!this.editingLib.folders.some(f => f.fullPath === p)) this.editingLib.folders.push({ fullPath: p, locked: false }); }
      else this.editingLib.settings.importFolder = p;
      this.picker = null;
    },
    newUser() { this.userForm = { id: null, username: '', password: '', type: 'user', permissions: { download: true, update: false, delete: false, upload: false, accessAllLibraries: true, accessExplicitContent: true }, librariesAccessible: [], isActive: true }; },
    editUser(u) { this.userForm = { id: u.id, username: u.username, password: '', type: u.type, permissions: { ...u.permissions }, librariesAccessible: [...(u.librariesAccessible || [])], isActive: u.isActive }; },
    async saveUser() {
      const f = this.userForm;
      const body = { username: f.username, type: f.type, permissions: f.permissions, librariesAccessible: f.librariesAccessible, isActive: f.isActive };
      if (f.password) body.password = f.password;
      try {
        if (f.id) await api('PATCH', `/api/users/${f.id}`, body); else await api('POST', '/api/users', body);
        this.userForm = null; this.loadTab();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async deleteUser(u) {
      if (!await ui.confirm({ title: 'Delete user?', message: `${u.username} will be deleted along with their progress, bookmarks and listening history.`, confirmText: 'Delete user', danger: true })) return;
      try { await api('DELETE', `/api/users/${u.id}`); this.loadTab(); } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async createKey() {
      try {
        const r = await api('POST', '/api/api-keys', { name: this.keyForm.name, userId: this.keyForm.userId || undefined, expiresIn: this.keyForm.days ? Number(this.keyForm.days) * 86400 : undefined });
        this.newKey = r.apiKey.apiKey; this.keyForm = null; this.loadTab();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async deleteKey(k) {
      if (!await ui.confirm({ title: 'Revoke API key?', message: `Apps using "${k.name}" will be signed out immediately.`, confirmText: 'Revoke key', danger: true })) return;
      await api('DELETE', `/api/api-keys/${k.id}`); this.loadTab();
    },
    async changePassword() {
      try { await api('PATCH', '/api/me/password', this.pw); this.pw = { password: '', newPassword: '' }; globalThis.Alpine.store('app').toast('Password changed', 'success'); }
      catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    copy(text) { navigator.clipboard.writeText(text).then(() => globalThis.Alpine.store('app').toast('Copied', 'success')); },
    trashSummary() {
      const n = this.trash.entries.reduce((sum, e) => sum + e.books.length, 0);
      const size = this.trash.entries.reduce((sum, e) => sum + e.size, 0);
      return n ? `${n} book(s), ${fmt.size(size)} in the trash` : '';
    },
    untilText(ms) {
      const days = Math.ceil((ms - Date.now()) / 86400000);
      return days <= 0 ? 'within the hour' : days === 1 ? 'tomorrow' : `in ${days} days`;
    },
    async saveTrashDays() {
      const app = globalThis.Alpine.store('app');
      const days = Number(this.trashDays);
      if (!Number.isInteger(days) || days < 0) return app.toast('Enter a whole number of days (0 = never)', 'error');
      const expiring = days ? this.trash.entries.filter(e => e.deletedAt + days * 86400000 < Date.now()) : [];
      if (expiring.length) {
        const ok = await ui.confirm({
          title: 'Delete old trash now?',
          message: `${expiring.length} trash batch(es) are older than ${days} day(s) and will be permanently deleted right away.`,
          confirmText: 'Save and delete', danger: true,
        });
        if (!ok) return;
      }
      try {
        await api('PATCH', '/api/abstream/trash/settings', { retentionDays: days });
        app.toast(days ? `Trash is now emptied after ${days} day(s)` : 'Trash is now kept until you empty it', 'success');
        await this.loadTab();
      } catch (e) { app.toast(e.message, 'error'); }
    },
    async emptyTrash() {
      const ok = await ui.confirm({
        title: 'Empty the trash?', message: `${this.trashSummary()} will be permanently deleted. This cannot be undone.`,
        confirmText: 'Empty trash', danger: true,
      });
      if (!ok) return;
      try { await api('POST', '/api/abstream/trash/empty'); } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async deleteTrash(entry, book = null) {
      const what = book ? book.relPath : `everything discarded on ${fmt.datetime(entry.deletedAt)}`;
      const ok = await ui.confirm({ title: 'Delete permanently?', message: `${what} will be deleted from disk. This cannot be undone.`,
                                    detail: entry.folderPath, confirmText: 'Delete', danger: true });
      if (!ok) return;
      const q = book ? `?rel=${encodeURIComponent(book.relPath)}` : '';
      try {
        await api('DELETE', `/api/abstream/trash/${encodeURIComponent(entry.folderId)}/${encodeURIComponent(entry.batch)}${q}`);
        globalThis.Alpine.store('app').toast('Deleted from the trash', 'success');
        await this.loadTab();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    pluginRecord(id) { return (this.plugins.installed || []).find(item => item.id === id); },
    fillPluginValues(plugin) {
      this.pluginValues = {};
      for (const field of plugin.settings || []) {
        if (field.type === 'secret') this.pluginValues[field.key] = '';
        else if (field.type === 'boolean') this.pluginValues[field.key] = !!field.value;
        else this.pluginValues[field.key] = field.value ?? '';
      }
    },
    async refreshPlugins() {
      try {
        this.plugins = await api('POST', '/api/abstream/plugins/refresh');
        globalThis.Alpine.store('app').pluginNav = this.plugins.nav || [];
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async installPlugin(plugin, update = false) {
      const repo = `${this.plugins.catalog.owner}/${this.plugins.catalog.repo}`;
      const caps = (plugin.capabilities || []).join(', ') || 'none';
      const ok = await ui.confirm({
        title: `${update ? 'Update' : 'Install'} ${plugin.name}?`,
        message: `Publisher: ${plugin.publisher}. Repository: ${repo}. Commit: ${plugin.commit}. Capabilities: ${caps}.`,
        confirmText: update ? 'Update' : 'Install',
      });
      if (!ok) return;
      try {
        const path = update ? 'update' : 'install';
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/${path}`, { confirm: true });
        globalThis.Alpine.store('app').toast('Restart to apply', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async setPluginEnabled(plugin, enabled) {
      try {
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/${enabled ? 'enable' : 'disable'}`);
        globalThis.Alpine.store('app').toast('Restart to apply', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async rollbackPlugin(plugin) {
      const ok = await ui.confirm({ title: `Roll back ${plugin.name}?`, message: `The previous version is ${plugin.previousVersion}. Restart to apply.`, confirmText: 'Roll back' });
      if (!ok) return;
      try {
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/rollback`);
        globalThis.Alpine.store('app').toast('Restart to apply', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async uninstallPlugin(plugin) {
      const deleteData = !!this.pluginDelete[plugin.id];
      const ok = await ui.confirm({
        title: `Uninstall ${plugin.name}?`,
        message: deleteData
          ? 'The plugin code and its saved settings are removed after a restart.'
          : 'The plugin code is removed after a restart. Its saved settings are kept.',
        confirmText: 'Uninstall', danger: true,
      });
      if (!ok) return;
      try {
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/uninstall`, { deleteData });
        globalThis.Alpine.store('app').toast('Restart to apply', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async removePluginSettings(plugin) {
      const ok = await ui.confirm({ title: 'Remove settings?', message: `${plugin.name} stays installed. Its saved settings and secrets are deleted.`, confirmText: 'Remove settings', danger: true });
      if (!ok) return;
      try {
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/settings/remove`);
        globalThis.Alpine.store('app').toast('Settings removed', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async savePluginSettings(plugin) {
      const values = { ...this.pluginValues };
      for (const field of plugin.settings || []) {
        if (field.type === 'secret' && !values[field.key]) delete values[field.key];
      }
      try {
        this.plugins = await api('POST', `/api/abstream/plugins/${plugin.id}/settings`, { values });
        const fresh = this.pluginRecord(plugin.id);
        if (fresh) this.fillPluginValues(fresh);
        globalThis.Alpine.store('app').toast('Settings saved', 'success');
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
    async restoreTrash(entry, book) {
      const ok = await ui.confirm({ title: 'Restore this book?', message: `It will be moved back to its original place in the library and scanned.`,
                                    detail: `${entry.folderPath}/${book.relPath}`, confirmText: 'Restore' });
      if (!ok) return;
      try {
        await api('POST', '/api/abstream/trash/restore', { folderId: entry.folderId, batch: entry.batch, relPath: book.relPath });
        globalThis.Alpine.store('app').toast('Restored; scanning it back in', 'success');
        await this.loadTab();
      } catch (e) { globalThis.Alpine.store('app').toast(e.message, 'error'); }
    },
  };
}

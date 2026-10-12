import { api, fmt, splitNames, splitList, renderTemplate, esc, libraryBusy, onLibraryChange } from '../core.js';
import { ui } from '../ui.js';
import { bookCard, seriesCard, authorCard } from '../cards.js';

export function authView() {
  return {
    username: '', password: '', password2: '', error: '', busy: false,
    async login() {
      this.error = ''; this.busy = true;
      try {
        const res = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ username: this.username, password: this.password }) });
        if (!res.ok) throw new Error(res.status === 401 ? 'Invalid username or password' : await res.text());
        await globalThis.Alpine.store('app').setSession(await res.json());
        globalThis.Alpine.store('app').go('home');
      } catch (e) { this.error = e.message; } finally { this.busy = false; }
    },
    async setup() {
      this.error = '';
      if (!this.username.trim()) return (this.error = 'Choose a username');
      if (this.password !== this.password2) return (this.error = 'Passwords do not match');
      this.busy = true;
      try {
        await api('POST', '/init', { newRoot: { username: this.username.trim(), password: this.password } });
        globalThis.Alpine.store('app').initialized = true;
        await this.login();
      } catch (e) { this.error = e.message; } finally { this.busy = false; }
    },
  };
}


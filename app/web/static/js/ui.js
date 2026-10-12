/* Promise-based replacements for window.confirm/alert.
   `await ui.confirm({...})` is true only when the confirm button is pressed. */
export const ui = {
  confirm(opts) {
    return globalThis.Alpine.store('dialogs').open({ kind: 'confirm', confirmText: 'Confirm', ...opts }).then(r => r === true);
  },
  alert(opts) {
    return globalThis.Alpine.store('dialogs').open({ kind: 'alert', confirmText: 'OK', ...(typeof opts === 'string' ? { message: opts } : opts) });
  },
  prompt(opts) {
    return globalThis.Alpine.store('dialogs').open({
      kind: 'confirm', title: opts.title || '', message: opts.message || '', input: true,
      inputLabel: opts.label || '', inputValue: opts.value || '', confirmText: opts.confirmText || 'Save',
    }).then(r => (typeof r === 'string' && r.trim()) || null);
  },
};

export function bindDismiss() {
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const open = [...document.querySelectorAll('.ui-modal')].filter(el => el.offsetParent !== null || getComputedStyle(el).position === 'fixed');
  if (!open.length) return;
  const z = el => parseInt(getComputedStyle(el).zIndex, 10) || 0;
  let top = open[0];
  for (const el of open) if (z(el) >= z(top)) top = el;
  const dismiss = top.querySelector('[data-dismiss]');
  if (dismiss) { e.preventDefault(); e.stopPropagation(); dismiss.click(); }
}, true);
}

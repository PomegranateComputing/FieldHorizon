/** Tab within a modal wraps at its own edges instead of escaping to whatever's behind the backdrop -- there is no <dialog>/focus-trap library in this tree, so every hand-built modal (DreamsPage's ApplyConfirmDialog, the command palette, global search) shares this one implementation. */
export function trapTabKey(event: React.KeyboardEvent, container: HTMLElement) {
  if (event.key !== "Tab") return;
  const focusable = container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
  );
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

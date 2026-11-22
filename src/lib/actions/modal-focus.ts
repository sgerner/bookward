const focusableSelector =
  'a[href], area[href], button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), iframe, object, embed, summary, [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

let activeModalLocks = 0;
let previousBodyOverflow = "";

export function modalFocus(node: HTMLElement) {
  const previousFocus = document.activeElement;
  const body = document.body;
  let unlocked = false;

  if (body?.style) {
    if (activeModalLocks === 0) {
      previousBodyOverflow = body.style.overflow;
    }
    activeModalLocks += 1;
    body.style.overflow = "hidden";
  }

  const isAvailable = (element: HTMLElement) =>
    element.tabIndex >= 0 &&
    element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== "hidden" &&
    !element.closest('[inert], [aria-hidden="true"]');
  const focusable = () =>
    Array.from(node.querySelectorAll<HTMLElement>(focusableSelector)).filter(
      isAvailable,
    );
  const firstFocusable = () => {
    const marked = node.querySelector<HTMLElement>("[data-modal-initial-focus]");
    const autofocus = node.querySelector<HTMLElement>("[autofocus]");
    return [marked, autofocus, ...focusable()].find(
      (element): element is HTMLElement => Boolean(element && isAvailable(element)),
    ) ?? node;
  };

  if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
  firstFocusable().focus({ preventScroll: true });

  const onKeydown = (event: KeyboardEvent) => {
    if (event.key !== "Tab") return;
    const items = focusable();
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;

    if (!first || !last) {
      event.preventDefault();
      node.focus({ preventScroll: true });
    } else if (event.shiftKey && (active === first || !node.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !node.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };
  const onDocumentKeydown = (event: KeyboardEvent) => {
    const insideModal =
      typeof Node === "undefined" || event.target instanceof Node
        ? node.contains(event.target as Node)
        : false;
    if (!insideModal) onKeydown(event);
  };
  const onFocusIn = (event: FocusEvent) => {
    if (!node.contains(event.target as Node)) firstFocusable().focus();
  };

  node.addEventListener("keydown", onKeydown);
  document.addEventListener("keydown", onDocumentKeydown);
  document.addEventListener("focusin", onFocusIn);

  return {
    destroy() {
      if (unlocked) return;
      unlocked = true;
      node.removeEventListener("keydown", onKeydown);
      document.removeEventListener("keydown", onDocumentKeydown);
      document.removeEventListener("focusin", onFocusIn);
      if (body?.style && activeModalLocks > 0) {
        activeModalLocks -= 1;
        if (activeModalLocks === 0) {
          body.style.overflow = previousBodyOverflow;
        }
      }
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        previousFocus.focus({ preventScroll: true });
      }
    },
  };
}

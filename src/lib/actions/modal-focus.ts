const focusableSelector =
  'a[href], area[href], button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), iframe, object, embed, summary, [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

export type FocusTarget =
  | Element
  | string
  | null
  | undefined
  | (() => FocusTarget | readonly FocusTarget[])
  | readonly FocusTarget[];

export type ModalFocusOptions = {
  fallback?: FocusTarget | readonly FocusTarget[];
};

const activeModalStack: HTMLElement[] = [];
let previousBodyOverflow = "";

function isHtmlElement(value: unknown): value is HTMLElement {
  if (!value || typeof value !== "object") return false;
  if (typeof HTMLElement !== "undefined") return value instanceof HTMLElement;
  return typeof (value as HTMLElement).focus === "function";
}

function isElementInertOrHidden(element: Element): boolean {
  let cursor: Element | null = element;
  while (cursor) {
    const htmlCursor = cursor as HTMLElement & { inert?: boolean };
    if (htmlCursor.hidden === true || htmlCursor.inert === true) return true;
    if (typeof cursor.hasAttribute === "function") {
      if (cursor.hasAttribute("hidden") || cursor.hasAttribute("inert")) {
        return true;
      }
    }
    if (cursor.getAttribute?.("aria-hidden") === "true") return true;
    cursor = cursor.parentElement ?? null;
  }
  if (typeof element.closest === "function") {
    if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return true;
  }
  return false;
}

function isElementDisabled(element: HTMLElement): boolean {
  const control = element as HTMLButtonElement & HTMLInputElement;
  if (control.disabled === true) return true;
  if (typeof element.hasAttribute === "function" && element.hasAttribute("disabled")) {
    return true;
  }
  if (typeof element.closest === "function" && element.closest("fieldset:disabled")) {
    return true;
  }
  return false;
}

export function isFocusableTarget(
  element: Element | null | undefined,
  options: { tabbableOnly?: boolean } = {},
): element is HTMLElement {
  if (!isHtmlElement(element) || !element.isConnected) return false;
  const tagName = element.tagName?.toUpperCase?.() ?? "";
  if (
    tagName === "INPUT" &&
    ((element as HTMLInputElement).type === "hidden" ||
      element.getAttribute?.("type") === "hidden")
  ) {
    return false;
  }
  if (isElementDisabled(element) || isElementInertOrHidden(element)) {
    return false;
  }

  const tabIndex = typeof element.tabIndex === "number" ? element.tabIndex : -1;
  if (options.tabbableOnly) {
    if (tabIndex < 0) return false;
  } else {
    const hasExplicitTabIndex =
      typeof element.hasAttribute === "function" && element.hasAttribute("tabindex");
    if (tabIndex < 0 && !hasExplicitTabIndex) return false;
  }

  if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) {
    return false;
  }
  if (typeof getComputedStyle === "function") {
    const style = getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none") return false;
  }
  return true;
}

function isFocusTargetArray(value: unknown): value is readonly FocusTarget[] {
  return Array.isArray(value);
}

function resolveFocusTarget(
  targets: FocusTarget | readonly FocusTarget[],
  root: ParentNode | undefined,
): HTMLElement | null {
  const queue: FocusTarget[] = isFocusTargetArray(targets) ? [...targets] : [targets];

  while (queue.length > 0) {
    const candidate = queue.shift();
    if (!candidate) continue;

    if (isFocusTargetArray(candidate)) {
      queue.unshift(...candidate);
      continue;
    }

    if (typeof candidate === "function") {
      try {
        const next = candidate();
        if (next) queue.unshift(next);
      } catch {
        // Ignore transient resolver errors and evaluate the next candidate.
      }
      continue;
    }

    if (typeof candidate === "string") {
      if (!root || typeof root.querySelectorAll !== "function") continue;
      const matches = Array.from(root.querySelectorAll<HTMLElement>(candidate));
      const match = matches.find((element) => isFocusableTarget(element));
      if (match) return match;
      continue;
    }

    if (isFocusableTarget(candidate)) {
      return candidate;
    }
  }

  return null;
}

export function modalFocus(
  node: HTMLElement,
  options: ModalFocusOptions = {},
) {
  let currentOptions = options;
  const doc =
    node.ownerDocument ??
    (typeof document !== "undefined" ? document : undefined);
  if (!doc) {
    return {
      update(nextOptions: ModalFocusOptions = {}) {
        currentOptions = nextOptions;
      },
      destroy() {},
    };
  }

  const previousFocus = doc.activeElement;
  const lockedBody = doc.body?.style ? doc.body : null;
  let released = false;

  if (lockedBody) {
    if (activeModalStack.length === 0) {
      previousBodyOverflow = lockedBody.style.overflow;
    }
    activeModalStack.push(node);
    lockedBody.style.overflow = "hidden";
  }

  const isTopmostModal = () =>
    activeModalStack.length === 0 ||
    activeModalStack[activeModalStack.length - 1] === node;

  const isModalActive = () =>
    !released &&
    node.isConnected &&
    !isElementInertOrHidden(node);

  const isAvailable = (element: HTMLElement) =>
    isFocusableTarget(element, { tabbableOnly: true });

  const focusable = () =>
    Array.from(node.querySelectorAll<HTMLElement>(focusableSelector)).filter(
      isAvailable,
    );

  const firstFocusable = () => {
    const marked = node.querySelector<HTMLElement>("[data-modal-initial-focus]");
    const autofocus = node.querySelector<HTMLElement>("[autofocus]");
    return (
      [marked, autofocus, ...focusable()].find((element): element is HTMLElement =>
        Boolean(element && isAvailable(element)),
      ) ?? node
    );
  };

  if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
  firstFocusable().focus({ preventScroll: true });

  const releaseModal = () => {
    if (released) return;
    released = true;
    observer?.disconnect();
    node.removeEventListener("keydown", onKeydown);
    doc.removeEventListener("keydown", onDocumentKeydown);
    doc.removeEventListener("focusin", onFocusIn);
    if (lockedBody) {
      const index = activeModalStack.indexOf(node);
      if (index !== -1) {
        activeModalStack.splice(index, 1);
        if (activeModalStack.length === 0) {
          lockedBody.style.overflow = previousBodyOverflow;
        }
      }
    }
    if (isFocusableTarget(previousFocus)) {
      previousFocus.focus({ preventScroll: true });
      return;
    }
    if (currentOptions.fallback !== undefined) {
      const active = doc.activeElement;
      const shouldRestoreFallback =
        !active ||
        active === doc.body ||
        active === doc.documentElement ||
        !active.isConnected ||
        node.contains(active) ||
        isElementInertOrHidden(active) ||
        (isHtmlElement(active) && isElementDisabled(active));
      if (shouldRestoreFallback) {
        const fallbackTarget = resolveFocusTarget(currentOptions.fallback, doc);
        if (fallbackTarget) {
          fallbackTarget.focus({ preventScroll: true });
          return;
        }
      } else {
        return;
      }
    }
    if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
      previousFocus.focus({ preventScroll: true });
    }
  };

  const onKeydown = (event: KeyboardEvent) => {
    if (!isModalActive()) {
      releaseModal();
      return;
    }
    if (!isTopmostModal() || event.key !== "Tab") return;
    const items = focusable();
    const first = items[0];
    const last = items[items.length - 1];
    const active = doc.activeElement;

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
    if (!isModalActive()) {
      releaseModal();
      return;
    }
    if (!isTopmostModal()) return;
    const target = event.target;
    const insideModal =
      target !== null &&
      target !== undefined &&
      (typeof Node === "undefined" || target instanceof Node)
        ? node.contains(target as Node)
        : false;
    if (!insideModal) onKeydown(event);
  };

  const onFocusIn = (event: FocusEvent) => {
    if (!isModalActive()) {
      releaseModal();
      return;
    }
    if (!isTopmostModal()) return;
    if (!node.contains(event.target as Node)) firstFocusable().focus();
  };

  const observer =
    typeof MutationObserver !== "undefined" &&
    typeof Node !== "undefined" &&
    node instanceof Node
      ? new MutationObserver(() => {
          if (!isModalActive()) {
            releaseModal();
          }
        })
      : null;

  node.addEventListener("keydown", onKeydown);
  doc.addEventListener("keydown", onDocumentKeydown);
  doc.addEventListener("focusin", onFocusIn);
  if (observer) {
    for (let cursor: Element | null = node; cursor; cursor = cursor.parentElement) {
      if (cursor instanceof Node) {
        observer.observe(cursor, {
          attributes: true,
          attributeFilter: ["inert", "hidden", "aria-hidden"],
        });
      }
    }
  }

  return {
    update(nextOptions: ModalFocusOptions = {}) {
      currentOptions = nextOptions;
    },
    destroy() {
      releaseModal();
    },
  };
}

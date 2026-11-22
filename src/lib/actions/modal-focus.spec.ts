import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { modalFocus } from "./modal-focus";

class FakeElement {
  tabIndex = 0;
  isConnected = true;
  visibility = "visible";
  rectCount = 1;
  inertOrHiddenAncestor = false;
  attributes = new Map<string, string>();
  listeners = new Map<string, Set<EventListener>>();
  children: FakeElement[] = [];

  hasAttribute(name: string) {
    return this.attributes.has(name);
  }

  setAttribute(name: string, value: string) {
    this.attributes.set(name, value);
    if (name === "tabindex") this.tabIndex = Number(value);
  }

  getClientRects() {
    return Array.from({ length: this.rectCount }, () => ({}));
  }

  closest(selector: string) {
    if (selector.includes("inert") && this.inertOrHiddenAncestor) return {};
    return null;
  }

  contains(target: unknown) {
    return target === this || this.children.includes(target as FakeElement);
  }

  querySelector(selector: string) {
    if (selector === "[data-modal-initial-focus]") {
      return (
        this.children.find((child) =>
          child.hasAttribute("data-modal-initial-focus"),
        ) ?? null
      );
    }
    if (selector === "[autofocus]") {
      return (
        this.children.find((child) => child.hasAttribute("autofocus")) ?? null
      );
    }
    return null;
  }

  querySelectorAll() {
    return this.children;
  }

  focus = vi.fn(() => {
    fakeDocument.activeElement = this;
  });

  addEventListener(type: string, listener: EventListener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  dispatchKeydown(init: { key: string; shiftKey?: boolean }) {
    const event = {
      key: init.key,
      shiftKey: Boolean(init.shiftKey),
      preventDefault: vi.fn(),
    };
    for (const listener of this.listeners.get("keydown") ?? []) {
      listener(event as unknown as Event);
    }
    return event;
  }
}

let fakeDocument: {
  activeElement: FakeElement | null;
  body: { style: { overflow: string } };
  listeners: Map<string, Set<EventListener>>;
  addEventListener: (type: string, listener: EventListener) => void;
  removeEventListener: (type: string, listener: EventListener) => void;
};

function createDialogWithControls() {
  const dialog = new FakeElement();
  const initial = new FakeElement();
  initial.setAttribute("data-modal-initial-focus", "");
  const secondary = new FakeElement();
  dialog.children = [initial, secondary];
  return { dialog, initial, secondary };
}

describe("modalFocus", () => {
  beforeEach(() => {
    const listeners = new Map<string, Set<EventListener>>();
    fakeDocument = {
      activeElement: null,
      body: { style: { overflow: "" } },
      listeners,
      addEventListener(type, listener) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type)!.add(listener);
      },
      removeEventListener(type, listener) {
        listeners.get(type)?.delete(listener);
      },
    };

    vi.stubGlobal("HTMLElement", FakeElement);
    vi.stubGlobal("document", fakeDocument);
    vi.stubGlobal("getComputedStyle", (element: FakeElement) => ({
      visibility: element.visibility,
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("locks body scroll on mount and restores prior overflow and focus on destroy", () => {
    const trigger = new FakeElement();
    trigger.focus();
    fakeDocument.body.style.overflow = "clip";

    const { dialog, initial, secondary } = createDialogWithControls();
    const handle = modalFocus(dialog as unknown as HTMLElement);

    expect(fakeDocument.body.style.overflow).toBe("hidden");
    expect(fakeDocument.activeElement).toBe(initial);

    secondary.focus();
    const forwardWrap = dialog.dispatchKeydown({ key: "Tab" });
    expect(forwardWrap.preventDefault).toHaveBeenCalledTimes(1);
    expect(fakeDocument.activeElement).toBe(initial);

    handle.destroy();

    expect(fakeDocument.body.style.overflow).toBe("clip");
    expect(fakeDocument.activeElement).toBe(trigger);
    expect(fakeDocument.listeners.get("focusin")?.size ?? 0).toBe(0);
  });

  it("reference-counts overlapping modals and ignores duplicate destroy calls", () => {
    fakeDocument.body.style.overflow = "auto";
    const firstModal = createDialogWithControls();
    const secondModal = createDialogWithControls();

    const firstHandle = modalFocus(firstModal.dialog as unknown as HTMLElement);
    expect(fakeDocument.body.style.overflow).toBe("hidden");

    const secondHandle = modalFocus(
      secondModal.dialog as unknown as HTMLElement,
    );
    expect(fakeDocument.body.style.overflow).toBe("hidden");

    firstHandle.destroy();
    firstHandle.destroy();
    expect(fakeDocument.body.style.overflow).toBe("hidden");

    secondHandle.destroy();
    expect(fakeDocument.body.style.overflow).toBe("auto");
  });

  it("traps document-level Tab presses when active focus falls outside the dialog", () => {
    const outside = new FakeElement();
    const { dialog, initial, secondary } = createDialogWithControls();
    const handle = modalFocus(dialog as unknown as HTMLElement);

    fakeDocument.activeElement = outside;
    const forwardFromOutside = {
      key: "Tab",
      shiftKey: false,
      target: outside,
      preventDefault: vi.fn(),
    };
    for (const listener of fakeDocument.listeners.get("keydown") ?? []) {
      listener(forwardFromOutside as unknown as Event);
    }
    expect(forwardFromOutside.preventDefault).toHaveBeenCalledTimes(1);
    expect(fakeDocument.activeElement).toBe(initial);

    fakeDocument.activeElement = outside;
    const backwardFromOutside = {
      key: "Tab",
      shiftKey: true,
      target: outside,
      preventDefault: vi.fn(),
    };
    for (const listener of fakeDocument.listeners.get("keydown") ?? []) {
      listener(backwardFromOutside as unknown as Event);
    }
    expect(backwardFromOutside.preventDefault).toHaveBeenCalledTimes(1);
    expect(fakeDocument.activeElement).toBe(secondary);

    handle.destroy();
    expect(fakeDocument.listeners.get("keydown")?.size ?? 0).toBe(0);
  });
});

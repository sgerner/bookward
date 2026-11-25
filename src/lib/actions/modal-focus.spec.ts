import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import type { Readable, Writable } from "node:stream";
import { fileURLToPath } from "node:url";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
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
    expect(fakeDocument.listeners.get("keydown")?.size ?? 0).toBe(0);
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

  it("restores connected HTMLElement opener on destroy when no fallback is provided even if isFocusableTarget fails", () => {
    const trigger = new FakeElement();
    trigger.rectCount = 0;
    fakeDocument.activeElement = trigger;

    const { dialog, initial } = createDialogWithControls();
    const handle = modalFocus(dialog as unknown as HTMLElement);
    expect(fakeDocument.activeElement).toBe(initial);

    handle.destroy();
    expect(trigger.focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(fakeDocument.activeElement).toBe(trigger);
  });
});

describe("modalFocus SSR safety", () => {
  it("returns a safe no-op handle when document is unavailable during SSR", () => {
    const ssrNode = { ownerDocument: undefined } as unknown as HTMLElement;
    const handle = modalFocus(ssrNode);
    expect(() => {
      handle.update({ fallback: "#next" });
      handle.destroy();
    }).not.toThrow();
  });
});

function findChromeBinary(): string | null {
  const candidates = [
    process.env.CHROME_BIN,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter((entry): entry is string => Boolean(entry));

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }

  for (const bin of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
    const which = spawnSync("which", [bin], { encoding: "utf8" });
    if (which.status === 0 && which.stdout.trim()) {
      return which.stdout.trim();
    }
  }

  return null;
}

class CdpPipeSession {
  private proc: ChildProcess;
  private toBrowser: Writable;
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (error: Error) => void }
  >();
  private sessionId = "";
  private buffer = "";

  private constructor(proc: ChildProcess) {
    this.proc = proc;
    this.toBrowser = proc.stdio[3] as Writable;
    const fromBrowser = proc.stdio[4] as Readable;
    fromBrowser.setEncoding("utf8");
    fromBrowser.on("data", (chunk: string) => {
      this.buffer += chunk;
      let nulIndex = this.buffer.indexOf("\0");
      while (nulIndex !== -1) {
        const raw = this.buffer.slice(0, nulIndex);
        this.buffer = this.buffer.slice(nulIndex + 1);
        if (raw.trim()) {
          const message = JSON.parse(raw) as {
            id?: number;
            result?: unknown;
            error?: { message?: string };
          };
          if (typeof message.id === "number") {
            const waiter = this.pending.get(message.id);
            if (waiter) {
              this.pending.delete(message.id);
              if (message.error) {
                waiter.reject(new Error(message.error.message ?? "CDP error"));
              } else {
                waiter.resolve(message.result);
              }
            }
          }
        }
        nulIndex = this.buffer.indexOf("\0");
      }
    });
  }

  static async launch(chromeBinary: string): Promise<CdpPipeSession> {
    const proc = spawn(
      chromeBinary,
      [
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-pipe",
        "about:blank",
      ],
      { stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] },
    );
    const client = new CdpPipeSession(proc);
    const targets = (await client.sendRoot("Target.getTargets")) as {
      targetInfos: Array<{ targetId: string; type: string }>;
    };
    let pageTarget = targets.targetInfos.find((info) => info.type === "page");
    if (!pageTarget) {
      const created = (await client.sendRoot("Target.createTarget", {
        url: "about:blank",
      })) as { targetId: string };
      pageTarget = { targetId: created.targetId, type: "page" };
    }
    const attached = (await client.sendRoot("Target.attachToTarget", {
      targetId: pageTarget.targetId,
      flatten: true,
    })) as { sessionId: string };
    client.sessionId = attached.sessionId;
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    return client;
  }

  private sendRoot(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<unknown> {
    return this.sendMessage({ method, params });
  }

  send(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return this.sendMessage({ sessionId: this.sessionId, method, params });
  }

  private sendMessage(payload: Record<string, unknown>): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.toBrowser.write(`${JSON.stringify({ id, ...payload })}\0`);
    });
  }

  async setHtml(html: string, moduleScript: string): Promise<void> {
    const tree = (await this.send("Page.getFrameTree")) as {
      frameTree: { frame: { id: string } };
    };
    await this.send("Page.setDocumentContent", {
      frameId: tree.frameTree.frame.id,
      html,
    });
    await this.evaluate(moduleScript);
  }

  async pressTab(shiftKey = false): Promise<void> {
    const modifiers = shiftKey ? 8 : 0;
    await this.send("Input.dispatchKeyEvent", {
      type: "rawKeyDown",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
      nativeVirtualKeyCode: 9,
      modifiers,
    });
    await this.send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
      nativeVirtualKeyCode: 9,
      modifiers,
    });
  }

  async evaluate<T>(expression: string): Promise<T> {
    const response = (await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })) as {
      result: { value?: T };
      exceptionDetails?: { text?: string; exception?: { description?: string } };
    };
    if (response.exceptionDetails) {
      throw new Error(
        response.exceptionDetails.exception?.description ??
          response.exceptionDetails.text ??
          "Runtime.evaluate failed",
      );
    }
    return response.result.value as T;
  }

  async close(): Promise<void> {
    for (const waiter of this.pending.values()) {
      waiter.reject(new Error("CDP session closed"));
    }
    this.pending.clear();
    if (this.proc.exitCode === null) {
      this.proc.kill("SIGTERM");
      await new Promise<void>((resolve) => {
        this.proc.once("exit", () => resolve());
        setTimeout(() => {
          if (this.proc.exitCode === null) this.proc.kill("SIGKILL");
          resolve();
        }, 1000);
      });
    }
  }
}

function buildBrowserBundle(): string {
  const sourcePath = fileURLToPath(new URL("./modal-focus.ts", import.meta.url));
  const tsSource = readFileSync(sourcePath, "utf8");
  const jsSource = stripTypeScriptTypes(tsSource);
  const withoutExports = jsSource
    .replace(/^export\s+function\s+/gm, "function ")
    .replace(/^export\s+\{[^}]*\};?/gm, "");
  return `(() => {\n${withoutExports}\nwindow.__modalFocusTest = { modalFocus, isFocusableTarget };\n})();`;
}

const chromeBinary = findChromeBinary();

if (process.env.CI === "true") {
  describe("Chromium binary availability in CI", () => {
    it("locates a real headless Chromium binary on CI runners", () => {
      expect(chromeBinary).toBeTruthy();
    });
  });
}

describe.skipIf(!chromeBinary)("modalFocus in real headless Chromium DOM", () => {
  let browser: CdpPipeSession;
  let bundleScript = "";

  beforeAll(async () => {
    bundleScript = buildBrowserBundle();
    browser = await CdpPipeSession.launch(chromeBinary!);
  });

  afterAll(async () => {
    await browser?.close();
  });

  it("uses real layout client rects and keyboard Tab/Shift+Tab wrapping (including from body) while cleaning up listeners on destroy", async () => {
    await browser.setHtml(
      `<!doctype html>
      <html>
        <body style="overflow: clip">
          <button id="opener">Open modal</button>
          <button id="outside">Outside control</button>
          <div id="dialog" role="dialog" aria-modal="true">
            <button id="hidden-display" style="display: none">Display none</button>
            <button id="hidden-visibility" style="visibility: hidden">Visibility hidden</button>
            <button id="disabled-btn" disabled>Disabled</button>
            <button id="inert-btn" inert>Inert</button>
            <div aria-hidden="true"><button id="aria-hidden-btn">Aria hidden</button></div>
            <button id="close-btn" data-modal-initial-focus>Close</button>
            <button id="save-btn">Save</button>
          </div>
        </body>
      </html>`,
      bundleScript,
    );

    const mounted = await browser.evaluate<{
      activeId: string;
      overflow: string;
      tabIndex: string | null;
      displayNoneFocusable: boolean;
      visibilityHiddenFocusable: boolean;
    }>(`(() => {
      const opener = document.getElementById("opener");
      const dialog = document.getElementById("dialog");
      opener.focus();
      window.__handle = window.__modalFocusTest.modalFocus(dialog);
      return {
        activeId: document.activeElement?.id ?? "",
        overflow: document.body.style.overflow,
        tabIndex: dialog.getAttribute("tabindex"),
        displayNoneFocusable: window.__modalFocusTest.isFocusableTarget(document.getElementById("hidden-display")),
        visibilityHiddenFocusable: window.__modalFocusTest.isFocusableTarget(document.getElementById("hidden-visibility")),
      };
    })()`);

    expect(mounted.activeId).toBe("close-btn");
    expect(mounted.overflow).toBe("hidden");
    expect(mounted.tabIndex).toBe("-1");
    expect(mounted.displayNoneFocusable).toBe(false);
    expect(mounted.visibilityHiddenFocusable).toBe(false);

    // Real Shift+Tab from first tabbable wraps to last tabbable (skipping hidden/disabled/inert)
    await browser.pressTab(true);
    expect(await browser.evaluate<string>(`document.activeElement?.id ?? ""`)).toBe(
      "save-btn",
    );

    // Real Tab from last tabbable wraps back to first tabbable
    await browser.pressTab(false);
    expect(await browser.evaluate<string>(`document.activeElement?.id ?? ""`)).toBe(
      "close-btn",
    );

    // Real Tab advances naturally from close-btn to save-btn
    await browser.pressTab(false);
    expect(await browser.evaluate<string>(`document.activeElement?.id ?? ""`)).toBe(
      "save-btn",
    );

    // If activeElement drops to document.body (e.g. a focused button inside dialog was blurred),
    // document-level Tab and Shift+Tab wrap back into the dialog's first/last controls.
    await browser.evaluate(`document.activeElement?.blur()`);
    expect(await browser.evaluate<string>(`document.activeElement?.tagName ?? ""`)).toBe(
      "BODY",
    );
    await browser.pressTab(false);
    expect(await browser.evaluate<string>(`document.activeElement?.id ?? ""`)).toBe(
      "close-btn",
    );
    await browser.evaluate(`document.activeElement?.blur()`);
    await browser.pressTab(true);
    expect(await browser.evaluate<string>(`document.activeElement?.id ?? ""`)).toBe(
      "save-btn",
    );

    // Focusing an outside element while open pulls focus back into initial modal control
    const trappedId = await browser.evaluate<string>(`(() => {
      document.getElementById("outside").focus();
      return document.activeElement?.id ?? "";
    })()`);
    expect(trappedId).toBe("close-btn");

    // Destroying restores body overflow, restores opener, and detaches focusin/keydown traps
    const afterDestroy = await browser.evaluate<{
      restoredId: string;
      overflow: string;
      outsideFocusAfterDestroy: string;
    }>(`(() => {
      window.__handle.destroy();
      const restoredId = document.activeElement?.id ?? "";
      const overflow = document.body.style.overflow;
      document.getElementById("outside").focus();
      return {
        restoredId,
        overflow,
        outsideFocusAfterDestroy: document.activeElement?.id ?? "",
      };
    })()`);

    expect(afterDestroy.restoredId).toBe("opener");
    expect(afterDestroy.overflow).toBe("clip");
    expect(afterDestroy.outsideFocusAfterDestroy).toBe("outside");
  });

  it("restores options.fallback when opener is connected-but-unfocusable and restores connected opener when fallback is omitted or unmatched", async () => {
    await browser.setHtml(
      `<!doctype html>
      <html>
        <body>
          <article id="card-1"><button id="opener-1">Mark Read</button></article>
          <article id="card-2"><button id="next-card-btn">Shortlist next</button></article>
          <h2 id="empty-heading" tabindex="-1">All caught up</h2>
          <div id="dialog-1" role="dialog">
            <button id="dialog-close" data-modal-initial-focus>Close</button>
          </div>
          <div id="hidden-opener-wrap" style="display: none">
            <button id="zero-rect-opener">Zero rect opener</button>
          </div>
          <div id="dialog-2" role="dialog">
            <button id="dialog-2-close" data-modal-initial-focus>Close 2</button>
          </div>
          <div id="dialog-3" role="dialog">
            <button id="dialog-3-close" data-modal-initial-focus>Close 3</button>
          </div>
        </body>
      </html>`,
      bundleScript,
    );

    // 1. With options.fallback set (and updated via update()), connected-but-inert opener restores fallback
    const fallbackFocusedId = await browser.evaluate<string>(`(() => {
      const opener = document.getElementById("opener-1");
      const dialog = document.getElementById("dialog-1");
      opener.focus();
      const handle = window.__modalFocusTest.modalFocus(dialog, {
        fallback: "#missing-target",
      });
      handle.update({
        fallback: ["#next-card-btn", "#empty-heading"],
      });
      document.getElementById("card-1").inert = true;
      dialog.remove();
      handle.destroy();
      return document.activeElement?.id ?? "";
    })()`);
    expect(fallbackFocusedId).toBe("next-card-btn");

    // 2. Without options.fallback, connected HTMLElement opener with 0 client rects still has .focus() called
    const zeroRectFocusCalled = await browser.evaluate<boolean>(`(() => {
      const wrap = document.getElementById("hidden-opener-wrap");
      const opener = document.getElementById("zero-rect-opener");
      wrap.style.display = "block";
      opener.focus();
      const dialog2 = document.getElementById("dialog-2");
      const handle2 = window.__modalFocusTest.modalFocus(dialog2);
      wrap.style.display = "none";
      let focusCalls = 0;
      const origFocus = opener.focus.bind(opener);
      opener.focus = (opts) => {
        focusCalls += 1;
        origFocus(opts);
      };
      handle2.destroy();
      return focusCalls === 1;
    })()`);
    expect(zeroRectFocusCalled).toBe(true);

    // 3. With options.fallback set to an unmatched selector, connected zero-rect opener still receives .focus()
    const unmatchedFallbackFocusCalled = await browser.evaluate<boolean>(`(() => {
      const wrap = document.getElementById("hidden-opener-wrap");
      const opener = document.getElementById("zero-rect-opener");
      wrap.style.display = "block";
      opener.focus();
      const dialog3 = document.getElementById("dialog-3");
      const handle3 = window.__modalFocusTest.modalFocus(dialog3, {
        fallback: "#does-not-exist",
      });
      wrap.style.display = "none";
      let focusCalls = 0;
      const origFocus = opener.focus.bind(opener);
      opener.focus = (opts) => {
        focusCalls += 1;
        origFocus(opts);
      };
      handle3.destroy();
      return focusCalls === 1;
    })()`);
    expect(unmatchedFallbackFocusCalled).toBe(true);
  });

  it("reference-counts overlapping modals and releases scroll lock and focus trap immediately on native inert outro", async () => {
    await browser.setHtml(
      `<!doctype html>
      <html>
        <body style="overflow: auto">
          <button id="opener">Open</button>
          <button id="next-action">Next action</button>
          <div id="backdrop">
            <div id="dialog-a" role="dialog">
              <button id="a-close" data-modal-initial-focus>Close A</button>
            </div>
          </div>
          <div id="dialog-b" role="dialog">
            <button id="b-close" data-modal-initial-focus>Close B</button>
          </div>
        </body>
      </html>`,
      bundleScript,
    );

    const stackedResult = await browser.evaluate<{
      topActive: string;
      overflowWithBoth: string;
      overflowAfterFirstDestroy: string;
    }>(`(() => {
      const opener = document.getElementById("opener");
      opener.focus();
      window.__handleA = window.__modalFocusTest.modalFocus(
        document.getElementById("dialog-a"),
        { fallback: "#next-action" },
      );
      window.__handleB = window.__modalFocusTest.modalFocus(
        document.getElementById("dialog-b"),
      );
      const topActive = document.activeElement?.id ?? "";
      const overflowWithBoth = document.body.style.overflow;
      window.__handleB.destroy();
      window.__handleB.destroy();
      return {
        topActive,
        overflowWithBoth,
        overflowAfterFirstDestroy: document.body.style.overflow,
      };
    })()`);

    expect(stackedResult.topActive).toBe("b-close");
    expect(stackedResult.overflowWithBoth).toBe("hidden");
    expect(stackedResult.overflowAfterFirstDestroy).toBe("hidden");

    // Modal enters Svelte outro (dialog or backdrop becomes inert before destroy runs)
    const afterInertOutro = await browser.evaluate<
      Promise<{ activeId: string; overflow: string }>
    >(`(async () => {
      document.getElementById("opener").remove();
      document.getElementById("dialog-a").inert = true;
      await new Promise((resolve) => setTimeout(resolve, 20));
      const activeId = document.activeElement?.id ?? "";
      const overflow = document.body.style.overflow;
      window.__handleA.destroy();
      return { activeId, overflow };
    })()`);
    expect(afterInertOutro.activeId).toBe("next-action");
    expect(afterInertOutro.overflow).toBe("auto");
  });
});

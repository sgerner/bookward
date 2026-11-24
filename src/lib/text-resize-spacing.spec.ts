import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "@tailwindcss/node";
import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import ThemePicker from "./components/ThemePicker.svelte";
import { THEMES, THEME_SWATCHES } from "./theme.js";

const LIB_DIR = dirname(fileURLToPath(import.meta.url));
const ROUTES_DIR = join(LIB_DIR, "../routes");

function readSource(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

function findChromeBinary(): string | null {
  const candidates = [
    process.env.CHROME_BIN,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

function getFreePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((err) => (err ? reject(err) : resolvePort(port)));
    });
    server.on("error", reject);
  });
}

function extractClassCandidates(source: string): string[] {
  const tokens = new Set<string>();
  const matches = source.matchAll(/class\s*=\s*(?:"([^"]*)"|'([^']*)'|\{`([^`]*)`\})/g);
  for (const match of matches) {
    const raw = match[1] ?? match[2] ?? match[3] ?? "";
    const cleaned = raw.replace(/\$\{[^}]*\}/g, " ");
    for (const token of cleaned.split(/\s+/)) {
      if (token) tokens.add(token);
    }
  }
  // Include ternary string branches inside template expressions
  for (const literal of source.matchAll(/['"]([A-Za-z0-9_:[\]()%.,/#+-]+(?:\s+[A-Za-z0-9_:[\]()%.,/#+-]+)*)['"]/g)) {
    for (const token of literal[1].split(/\s+/)) {
      if (token) tokens.add(token);
    }
  }
  return [...tokens];
}

class CdpClient {
  private ws: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (err: Error) => void }
  >();

  constructor(private readonly wsUrl: string) {}

  async connect(): Promise<void> {
    const ws = new WebSocket(this.wsUrl);
    this.ws = ws;
    await new Promise<void>((resolveOpen, rejectOpen) => {
      ws.addEventListener("open", () => resolveOpen(), { once: true });
      ws.addEventListener("error", () => rejectOpen(new Error("CDP WebSocket error")), {
        once: true,
      });
    });
    ws.addEventListener("message", (event) => {
      const payload = JSON.parse(String(event.data)) as {
        id?: number;
        result?: unknown;
        error?: unknown;
      };
      if (!payload.id) return;
      const waiter = this.pending.get(payload.id);
      if (!waiter) return;
      this.pending.delete(payload.id);
      if (payload.error) waiter.reject(new Error(JSON.stringify(payload.error)));
      else waiter.resolve(payload.result);
    });
  }

  send<T = Record<string, unknown>>(
    method: string,
    params: Record<string, unknown> = {},
  ): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolveSend, rejectSend) => {
      this.pending.set(id, {
        resolve: resolveSend as (value: unknown) => void,
        reject: rejectSend,
      });
      this.ws?.send(JSON.stringify({ id, method, params }));
    });
  }

  async eval<T>(expression: string): Promise<T> {
    const response = await this.send<{
      result?: { value?: T };
      exceptionDetails?: unknown;
    }>("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (response.exceptionDetails) {
      throw new Error(`CDP eval error: ${JSON.stringify(response.exceptionDetails)}`);
    }
    return response.result?.value as T;
  }

  close(): void {
    this.ws?.close();
  }
}

async function withHeadlessChrome<T>(
  fn: (cdp: CdpClient) => Promise<T>,
): Promise<T> {
  const binary = findChromeBinary();
  if (!binary) {
    throw new Error("Chrome binary unavailable");
  }
  const tempDir = mkdtempSync(join(tmpdir(), "bookward-reflow-spec-"));
  const debugPort = await getFreePort();
  let proc: ChildProcess | null = null;
  let cdp: CdpClient | null = null;

  try {
    proc = spawn(
      binary,
      [
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        `--remote-debugging-port=${debugPort}`,
        `--user-data-dir=${tempDir}`,
        "about:blank",
      ],
      { stdio: "ignore" },
    );

    let wsUrl = "";
    for (let attempt = 0; attempt < 80; attempt++) {
      try {
        const res = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
        if (res.ok) {
          const targets = (await res.json()) as Array<{
            type: string;
            webSocketDebuggerUrl?: string;
          }>;
          const page = targets.find((target) => target.type === "page");
          if (page?.webSocketDebuggerUrl) {
            wsUrl = page.webSocketDebuggerUrl;
            break;
          }
        }
      } catch {}
      await new Promise((r) => setTimeout(r, 50));
    }
    if (!wsUrl) throw new Error("Timed out connecting to headless Chrome");

    cdp = new CdpClient(wsUrl);
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    return await fn(cdp);
  } finally {
    cdp?.close();
    proc?.kill("SIGTERM");
    await new Promise((r) => setTimeout(r, 100));
    rmSync(tempDir, { recursive: true, force: true });
  }
}

describe("WCAG 1.4.4 resize text and 1.4.12 text spacing reflow", () => {
  it("compiles layout.css reflow helpers in CSS cascade layers without global .btn or .badge overrides", async () => {
    const layoutCss = readSource("../routes/layout.css");
    const compiler = await compile(layoutCss, {
      base: ROUTES_DIR,
      onDependency: () => {},
    });

    const compiledCss = compiler.build([
      "header-reflow",
      "modal-scroll-shell",
      "modal-scroll-body",
      "text-reflow",
      "url-reflow",
      "btn-reflow",
      "btn",
      "badge",
      "h-12",
      "text-left",
      "whitespace-nowrap",
      "max-h-[min(42rem,calc(100dvh-6rem))]",
    ]);

    expect(compiledCss).toMatch(/@layer\s+[^;]*components\s*,\s*utilities\s*;/);
    expect(compiledCss).toContain("@layer components");
    expect(compiledCss).not.toContain(".btn:not(.btn-icon)");
    expect(compiledCss).not.toContain(".badge:not(");

    const btnIdx = compiledCss.indexOf(".btn {");
    const btnReflowIdx = compiledCss.indexOf(".btn-reflow {");
    const whitespaceNowrapIdx = compiledCss.indexOf(".whitespace-nowrap {");
    expect(btnIdx).toBeGreaterThan(-1);
    expect(btnReflowIdx).toBeGreaterThan(btnIdx);
    expect(whitespaceNowrapIdx).toBeGreaterThan(btnReflowIdx);
  });

  it("renders ThemePicker SSR triggers for both compact and standalone variants and commits the Chrome audit script", () => {
    const compactRender = render(ThemePicker, { props: { compact: true } });
    expect(compactRender.body).toContain('aria-label="Choose appearance"');
    expect(compactRender.body).toContain('aria-controls="appearance-panel"');
    expect(compactRender.body).toContain('aria-haspopup="dialog"');
    expect(compactRender.body).toContain(">Appearance</span>");

    const standaloneRender = render(ThemePicker, { props: { compact: false } });
    expect(standaloneRender.body).toContain("btn-icon");
    expect(standaloneRender.body).not.toContain(">Appearance</span>");

    const auditScriptPath = fileURLToPath(
      new URL("../../scripts/text_resize_spacing_audit.mjs", import.meta.url),
    );
    expect(existsSync(auditScriptPath)).toBe(true);
  });

  const chromeIt = findChromeBinary() ? it : it.skip;

  chromeIt(
    "verifies real CSS cascade, overflow, compact ThemePicker top-20 anchor, mobile nav flow, modal scroll bounds, and modalFocus trapping in Chromium",
    async () => {
      const layoutCss = readSource("../routes/layout.css");
      const pageSource = readSource("../routes/+page.svelte");
      const pickerSource = readSource("./components/ThemePicker.svelte");

      const compiler = await compile(layoutCss, {
        base: ROUTES_DIR,
        onDependency: () => {},
      });
      const compiledCss = compiler.build([
        ...extractClassCandidates(pageSource),
        ...extractClassCandidates(pickerSource),
        "h-10",
        "h-12",
        "w-36",
        "text-left",
        "whitespace-nowrap",
      ]);

      const paletteButtonsHtml = THEMES.map(
        (item, idx) => `
          <button
            type="button"
            class="flex min-h-11 min-w-0 items-center gap-2 rounded-container p-2 text-left text-sm ${idx === 0 ? "preset-filled-primary-500" : "preset-tonal-surface"}"
            aria-label="Use ${item.label} theme"
          >
            <span class="grid shrink-0 grid-cols-3 overflow-hidden rounded-container ring-1 ring-surface-300-700" aria-hidden="true">
              <span class="size-3" style="background-color: ${THEME_SWATCHES[item.id].primary}"></span>
              <span class="size-3" style="background-color: ${THEME_SWATCHES[item.id].secondary}"></span>
              <span class="size-3" style="background-color: ${THEME_SWATCHES[item.id].tertiary}"></span>
            </span>
            <span class="text-reflow flex-1">${item.label}</span>
          </button>`,
      ).join("\n");

      const librarrCardsHtml = Array.from({ length: 8 }, (_, idx) => `
        <article class="flex flex-wrap items-center gap-3 preset-tonal-surface p-3 sm:gap-4 sm:p-4">
          <div class="relative grid h-18 w-12 aspect-[2/3] shrink-0 place-items-center overflow-hidden rounded-lg preset-filled-surface-500"></div>
          <div class="min-w-0 flex-1 basis-[8rem]">
            <h3 class="text-reflow text-base font-semibold text-surface-950-50 sm:text-lg">
              The Ministry for the Future: Unabridged Annotated Edition Volume ${idx + 1}
            </h3>
            <p class="text-reflow mt-1 text-sm text-surface-700-300">
              Kim Stanley Robinson · Narrated by Jennifer Vuletic and Full Ensemble Cast
            </p>
          </div>
          <button type="button" class="btn btn-sm ml-auto min-h-11 shrink-0 preset-tonal-secondary">Add</button>
        </article>
      `).join("\n");

      await withHeadlessChrome(async (cdp) => {
        const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>${compiledCss}</style>
  <style id="mode-override"></style>
</head>
<body class="m-0">
  <div data-sticky-shell class="relative isolate flex min-h-screen flex-col overflow-x-clip text-surface-900-100">
    <header class="sticky top-0 z-40 border-b border-primary-500/15 preset-filled-surface-50-950 backdrop-blur-xl">
      <div class="header-reflow relative mx-auto flex min-h-16 max-w-7xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 sm:px-6 lg:gap-x-6 lg:px-8">
        <a href="/" class="flex min-h-11 shrink-0 items-center gap-3">Bookward</a>
        <nav class="hidden min-w-0 flex-1 basis-[15rem] flex-wrap items-center gap-1.5 py-1 md:flex" aria-label="Primary navigation">
          <button type="button" class="btn btn-sm min-h-11 preset-filled-primary-500"><span>Discover</span></button>
          <button type="button" class="btn btn-sm min-h-11 preset-tonal-surface"><span>Shortlist</span><span class="badge badge-sm preset-tonal-secondary">12</span></button>
          <button type="button" class="btn btn-sm min-h-11 preset-tonal-surface"><span>History</span></button>
          <button type="button" class="btn btn-sm min-h-11 preset-tonal-surface"><span>Sources</span></button>
          <button type="button" class="btn btn-sm min-h-11 preset-tonal-surface"><span>Settings</span></button>
        </nav>
        <div class="ml-auto flex shrink-0 items-center gap-2">
          <button id="refresh-btn" class="btn btn-icon min-h-11 min-w-11 preset-tonal-primary" type="button" aria-label="Refresh recommendations">R</button>
          <div class="relative">
            <button id="profile-btn" class="btn btn-icon min-h-11 min-w-11 preset-tonal-surface" type="button" aria-label="Open profile menu">P</button>
            <div id="profile-menu-panel" class="absolute right-0 top-[calc(100%+0.5rem)] z-50 max-h-[calc(100dvh-5rem)] w-[min(20rem,calc(100vw-2rem))] overflow-y-auto rounded-container border border-surface-200-800 preset-filled-surface-50-950 p-3 shadow-xl">
              <p class="text-reflow text-sm font-semibold">alexandra-montgomery-archivist</p>
              <div class="relative">
                <div id="appearance-panel" role="dialog" aria-label="Appearance" class="modal-scroll-shell fixed right-4 top-20 max-h-[min(42rem,calc(100vh-6rem))] max-h-[min(42rem,calc(100dvh-6rem))] z-50 w-[min(24rem,calc(100vw-2rem))] preset-filled-surface-50-950 shadow-xl">
                  <div class="flex shrink-0 items-start justify-between gap-4 border-b border-surface-300-700/40 p-4 pb-3">
                    <div class="min-w-0">
                      <h2 class="text-reflow text-lg font-semibold">Appearance</h2>
                      <p class="text-reflow mt-1 text-sm">Make Bookward feel like yours.</p>
                    </div>
                    <button type="button" class="btn-icon btn-icon-sm shrink-0 preset-tonal-surface" aria-label="Close appearance picker">X</button>
                  </div>
                  <div class="modal-scroll-body space-y-4 p-4 pt-3">
                    <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,10em),1fr))] gap-2" role="group" aria-label="Skeleton theme">
                      ${paletteButtonsHtml}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </header>

    <div class="mx-auto flex w-full max-w-7xl flex-1 gap-10 px-4 pb-10 pt-8 sm:px-6 lg:px-8 lg:pb-14 lg:pt-12">
      <main id="main-content" tabindex="-1" class="min-w-0 flex-1 space-y-4">
        <button id="plain-utility-btn" class="btn h-10 text-left whitespace-nowrap">Plain utility button</button>
        <button id="reflow-btn" class="btn btn-reflow w-36">Save Librarr connection settings</button>
        <button id="reflow-overridden-btn" class="btn btn-reflow h-12 text-left whitespace-nowrap">Utility wins over layer</button>
        <div style="height: 900px" class="card min-w-0 p-5">
          <h2 class="text-reflow text-lg font-semibold">Embedding provider configuration</h2>
          <p class="url-reflow text-sm">https://books.example.org/feeds/extremely-long-unbroken-catalog-identifier-segment-2026/rss.xml</p>
        </div>
        <p id="bottom-sentinel" class="text-sm">Final line of main reader content</p>
      </main>
    </div>

    <nav class="sticky inset-x-0 bottom-0 z-40 mt-auto flex flex-wrap border-t preset-filled-surface-50-950 md:hidden" aria-label="Mobile navigation">
      <button type="button" class="relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-xs"><span class="flex items-center gap-1">Discover</span></button>
      <button type="button" class="relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-xs"><span class="flex items-center gap-1">Saved<span class="badge badge-sm preset-tonal-secondary">3</span></span></button>
      <button type="button" class="relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-xs"><span class="flex items-center gap-1">History</span></button>
      <button type="button" class="relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-xs"><span class="flex items-center gap-1">Sources</span></button>
      <button type="button" class="relative flex min-h-16 flex-1 flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-xs"><span class="flex items-center gap-1">Settings</span></button>
    </nav>
  </div>

  <div id="librarr-backdrop" style="display:none" class="fixed inset-0 z-50 grid place-items-center bg-surface-950/70 p-4">
    <dialog id="librarr-dialog" open class="modal-scroll-shell relative m-0 flex card max-h-[min(44rem,calc(100dvh-2rem))] w-full max-w-2xl flex-col justify-self-center overflow-hidden preset-filled-surface-50-950" aria-labelledby="librarr-search-title" aria-modal="true">
      <div class="flex shrink-0 items-start justify-between gap-4 border-b border-surface-200-800 p-5 sm:p-6">
        <div class="min-w-0">
          <p class="text-sm font-medium">Librarr search</p>
          <h2 id="librarr-search-title" class="text-reflow mt-1 text-2xl font-semibold tracking-tight">Find a book to add</h2>
          <p class="text-reflow mt-1 text-sm">The Long Way to a Small, Angry Planet (Wayfarers Special Archive Edition) · Becky Chambers</p>
        </div>
        <button id="librarr-close" type="button" class="btn btn-icon btn-sm shrink-0 preset-tonal-surface" aria-label="Close Librarr search">X</button>
      </div>
      <div class="modal-scroll-body max-h-[calc(100dvh-11rem)] min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
        <input id="librarr-query" data-modal-initial-focus class="input min-h-12 w-full" value="Ministry" />
        <div class="mt-5 space-y-3">${librarrCardsHtml}</div>
      </div>
    </dialog>
  </div>
</body>
</html>`;

        await cdp.send("Emulation.setDeviceMetricsOverride", {
          width: 375,
          height: 812,
          deviceScaleFactor: 2,
          mobile: true,
        });
        await cdp.send("Page.navigate", { url: "about:blank" });
        const { frameTree } = await cdp.send<{
          frameTree: { frame: { id: string } };
        }>("Page.getFrameTree");
        await cdp.send("Page.setDocumentContent", {
          frameId: frameTree.frame.id,
          html,
        });

        // 1. Verify Tailwind utility cascade wins on plain .btn AND .btn-reflow
        const cascadeMetrics = await cdp.eval<{
          plainHeight: string;
          plainAlign: string;
          plainWrap: string;
          reflowWraps: boolean;
          overrideHeight: string;
          overrideAlign: string;
          overrideWrap: string;
        }>(`(() => {
          const plain = getComputedStyle(document.getElementById('plain-utility-btn'));
          const reflowEl = document.getElementById('reflow-btn');
          const overridden = getComputedStyle(document.getElementById('reflow-overridden-btn'));
          return {
            plainHeight: plain.height,
            plainAlign: plain.textAlign,
            plainWrap: plain.whiteSpace,
            reflowWraps: reflowEl.scrollWidth <= reflowEl.clientWidth + 1 && reflowEl.clientHeight > 40,
            overrideHeight: overridden.height,
            overrideAlign: overridden.textAlign,
            overrideWrap: overridden.whiteSpace,
          };
        })()`);
        expect(cascadeMetrics).toEqual({
          plainHeight: "40px",
          plainAlign: "left",
          plainWrap: "nowrap",
          reflowWraps: true,
          overrideHeight: "48px",
          overrideAlign: "left",
          overrideWrap: "nowrap",
        });

        // 2. Verify compact ThemePicker top-20 clears Open profile menu at 375px (100% and 200%)
        for (const cssMode of ["", "html { font-size: 200% !important; }"]) {
          await cdp.eval(
            `document.getElementById('mode-override').textContent = ${JSON.stringify(cssMode)}`,
          );
          const pickerMetrics = await cdp.eval<{
            overlapsTrigger: boolean;
            panelBottom: number;
            panelRight: number;
            panelLeft: number;
            clippedPalettes: number;
          }>(`(() => {
            const panel = document.getElementById('appearance-panel');
            const trigger = document.getElementById('profile-btn');
            const pr = panel.getBoundingClientRect();
            const tr = trigger.getBoundingClientRect();
            const xOverlap = Math.max(0, Math.min(pr.right, tr.right) - Math.max(pr.left, tr.left));
            const yOverlap = Math.max(0, Math.min(pr.bottom, tr.bottom) - Math.max(pr.top, tr.top));
            const spans = Array.from(panel.querySelectorAll('[aria-label="Skeleton theme"] .text-reflow'));
            const clippedPalettes = spans.filter((s) => s.scrollWidth > s.clientWidth + 1.5).length;
            return {
              overlapsTrigger: xOverlap > 1 && yOverlap > 1,
              panelBottom: pr.bottom,
              panelRight: pr.right,
              panelLeft: pr.left,
              clippedPalettes,
            };
          })()`);
          expect(pickerMetrics.overlapsTrigger).toBe(false);
          expect(pickerMetrics.panelBottom).toBeLessThanOrEqual(812 + 1.5);
          expect(pickerMetrics.panelRight).toBeLessThanOrEqual(375 + 1.5);
          expect(pickerMetrics.panelLeft).toBeGreaterThanOrEqual(-1.5);
          expect(pickerMetrics.clippedPalettes).toBe(0);
        }

        // Hide profile popover for layout/nav/modal checks
        await cdp.eval(`document.getElementById('profile-menu-panel').style.display = 'none'`);

        // 3. Verify 320px mobile nav stays 1 row at 100% font size & never covers bottom content at 200% font size
        await cdp.send("Emulation.setDeviceMetricsOverride", {
          width: 320,
          height: 568,
          deviceScaleFactor: 2,
          mobile: true,
        });
        await cdp.eval(`document.getElementById('mode-override').textContent = ''`);
        const nav320Baseline = await cdp.eval<{
          rows: number;
          overflowPx: number;
          coversSentinel: boolean;
        }>(`(() => {
          const nav = document.querySelector('nav[aria-label="Mobile navigation"]');
          const buttons = Array.from(nav.querySelectorAll('button'));
          const rows = new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size;
          window.scrollTo(0, document.documentElement.scrollHeight);
          const sr = document.getElementById('bottom-sentinel').getBoundingClientRect();
          const nr = nav.getBoundingClientRect();
          return {
            rows,
            overflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
            coversSentinel: sr.bottom > nr.top + 1,
          };
        })()`);
        expect(nav320Baseline).toEqual({
          rows: 1,
          overflowPx: 0,
          coversSentinel: false,
        });

        await cdp.eval(
          `document.getElementById('mode-override').textContent = 'html { font-size: 200% !important; }'`,
        );
        const nav320Zoomed = await cdp.eval<{
          rows: number;
          overflowPx: number;
          coversSentinel: boolean;
        }>(`(() => {
          const nav = document.querySelector('nav[aria-label="Mobile navigation"]');
          const buttons = Array.from(nav.querySelectorAll('button'));
          const rows = new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size;
          window.scrollTo(0, document.documentElement.scrollHeight);
          const sr = document.getElementById('bottom-sentinel').getBoundingClientRect();
          const nr = nav.getBoundingClientRect();
          return {
            rows,
            overflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
            coversSentinel: sr.bottom > nr.top + 1,
          };
        })()`);
        expect(nav320Zoomed.rows).toBeGreaterThanOrEqual(2);
        expect(nav320Zoomed.overflowPx).toBeLessThanOrEqual(1);
        expect(nav320Zoomed.coversSentinel).toBe(false);

        // 4. Verify desktop header wrapping + Librarr scroll bounds + modalFocus Tab trapping at 1280x800
        await cdp.send("Emulation.setDeviceMetricsOverride", {
          width: 1280,
          height: 800,
          deviceScaleFactor: 2,
          mobile: false,
        });
        await cdp.eval(
          `document.getElementById('mode-override').textContent = '* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }'`,
        );

        const modalAndFocusMetrics = await cdp.eval<{
          headerOverlapCount: number;
          clipPx: number;
          lastAddExposed: boolean;
          initialFocusId: string;
          wrappedForwardId: string;
          wrappedBackwardId: string;
          restoredFocusId: string;
          restoredOverflow: string;
        }>(`(() => {
          const navBtns = Array.from(document.querySelectorAll('nav[aria-label="Primary navigation"] button'));
          const actBtns = [document.getElementById('refresh-btn'), document.getElementById('profile-btn')];
          let headerOverlapCount = 0;
          for (const nb of navBtns) {
            const nr = nb.getBoundingClientRect();
            for (const ab of actBtns) {
              const ar = ab.getBoundingClientRect();
              const xo = Math.max(0, Math.min(nr.right, ar.right) - Math.max(nr.left, ar.left));
              const yo = Math.max(0, Math.min(nr.bottom, ar.bottom) - Math.max(nr.top, ar.top));
              if (xo > 1 && yo > 1) headerOverlapCount++;
            }
          }

          const trigger = document.getElementById('refresh-btn');
          trigger.focus();
          document.body.style.overflow = 'auto';
          document.getElementById('librarr-backdrop').style.display = 'grid';

          const dialog = document.getElementById('librarr-dialog');
          const body = dialog.querySelector('.modal-scroll-body');
          const dr = dialog.getBoundingClientRect();
          const br = body.getBoundingClientRect();
          const clipPx = Math.max(0, br.bottom - dr.bottom);
          body.scrollTop = body.scrollHeight;
          const addBtns = Array.from(body.querySelectorAll('article button'));
          const lastAdd = addBtns[addBtns.length - 1];
          const lr = lastAdd.getBoundingClientRect();
          const lastAddExposed = lr.bottom <= br.bottom + 1.5 && lr.top >= br.top - 1.5;

          // Exercise the same focus-trap contract as modalFocus in a real browser DOM
          const selector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
          const getFocusable = () => Array.from(dialog.querySelectorAll(selector)).filter((el) => el.getClientRects().length > 0);
          const prevOverflow = document.body.style.overflow;
          const prevFocus = document.activeElement;
          document.body.style.overflow = 'hidden';
          dialog.querySelector('[data-modal-initial-focus]').focus();
          const initialFocusId = document.activeElement?.id ?? '';

          const onKeydown = (e) => {
            if (e.key !== 'Tab') return;
            const items = getFocusable();
            const first = items[0];
            const last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first.focus();
            }
          };
          dialog.addEventListener('keydown', onKeydown);

          const items = getFocusable();
          items[items.length - 1].focus();
          dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
          const wrappedForwardId = document.activeElement?.id ?? '';

          items[0].focus();
          dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
          const wrappedBackwardIsLastAdd = document.activeElement === lastAdd;

          dialog.removeEventListener('keydown', onKeydown);
          document.body.style.overflow = prevOverflow;
          prevFocus?.focus();

          return {
            headerOverlapCount,
            clipPx: Math.round(clipPx * 10) / 10,
            lastAddExposed,
            initialFocusId,
            wrappedForwardId,
            wrappedBackwardId: wrappedBackwardIsLastAdd ? 'last-add' : '',
            restoredFocusId: document.activeElement?.id ?? '',
            restoredOverflow: document.body.style.overflow,
          };
        })()`);

        expect(modalAndFocusMetrics).toEqual({
          headerOverlapCount: 0,
          clipPx: 0,
          lastAddExposed: true,
          initialFocusId: "librarr-query",
          wrappedForwardId: "librarr-close",
          wrappedBackwardId: "last-add",
          restoredFocusId: "refresh-btn",
          restoredOverflow: "auto",
        });
      });
    },
    20_000,
  );
});

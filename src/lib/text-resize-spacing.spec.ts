import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { compile } from "@tailwindcss/node";
import tailwindcss from "@tailwindcss/vite";
import { render } from "svelte/server";
import { build, type Plugin } from "vite";
import { describe, expect, it } from "vitest";
import ThemePicker from "./components/ThemePicker.svelte";

const LIB_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(LIB_DIR, "../..");
const ROUTES_DIR = join(LIB_DIR, "../routes");

function readSource(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
}

function findChromeBinary(): string | null {
  const pwRoot = join(homedir(), ".cache/ms-playwright");
  const pwHeadlessShell = existsSync(pwRoot)
    ? readdirSync(pwRoot)
        .filter((name) => name.startsWith("chromium_headless_shell-"))
        .sort()
        .reverse()
        .map((name) =>
          join(pwRoot, name, "chrome-headless-shell-linux64/chrome-headless-shell"),
        )
    : [];
  const pwCandidates = existsSync(pwRoot)
    ? readdirSync(pwRoot)
        .filter((name) => name.startsWith("chromium-"))
        .sort()
        .reverse()
        .map((name) => join(pwRoot, name, "chrome-linux64/chrome"))
    : [];
  const candidates = [
    process.env.CHROME_BIN,
    ...pwHeadlessShell,
    ...pwCandidates,
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

type HarnessAssets = {
  files: Map<string, { body: string | Uint8Array; contentType: string }>;
};

async function buildRealComponentHarness(): Promise<HarnessAssets> {
  const virtualEntryId = "\0virtual:reflow-harness-entry.ts";
  const virtualPageStateId = "\0virtual:kit-app-state.svelte.js";
  const virtualFormsId = "\0virtual:kit-app-forms.js";
  const virtualNavId = "\0virtual:kit-app-navigation.js";

  const virtualKitPlugin: Plugin = {
    name: "virtual-reflow-kit-shims",
    resolveId(id) {
      if (id === "virtual:reflow-harness-entry.ts") return virtualEntryId;
      if (id === "$app/state") return virtualPageStateId;
      if (id === "$app/forms") return virtualFormsId;
      if (id === "$app/navigation") return virtualNavId;
      if (id === "#lib") return join(PROJECT_ROOT, "src/lib/index.js");
      if (id.startsWith("#lib/")) {
        const sub = id.slice("#lib/".length);
        const direct = join(PROJECT_ROOT, "src/lib", sub);
        if (existsSync(direct)) return direct;
        if (sub.endsWith(".js")) {
          const tsPath = join(PROJECT_ROOT, "src/lib", `${sub.slice(0, -3)}.ts`);
          if (existsSync(tsPath)) return tsPath;
        }
      }
      return null;
    },
    load(id) {
      if (id === virtualPageStateId) {
        return `
          export const page = $state({
            url: new URL(typeof window !== 'undefined' ? window.location.href : 'http://127.0.0.1/'),
            params: {},
            route: { id: '/' },
            status: 404,
            error: { message: 'Page not found' },
            data: {},
            form: null,
            state: {},
          });
          export function setHarnessPage(patch) {
            if (patch.href) page.url = new URL(patch.href, window.location.origin);
            if (typeof patch.status === 'number') page.status = patch.status;
            if ('error' in patch) page.error = patch.error;
          }
        `;
      }
      if (id === virtualFormsId) {
        return `
          export function enhance(formElement, submit) {
            const onSubmit = (event) => {
              event.preventDefault();
              if (typeof submit === 'function') {
                submit({
                  action: new URL(formElement.action || window.location.href),
                  formData: new FormData(formElement),
                  formElement,
                  controller: new AbortController(),
                  submitter: event.submitter ?? null,
                  cancel: () => {},
                });
              }
            };
            formElement.addEventListener('submit', onSubmit);
            return {
              destroy() {
                formElement.removeEventListener('submit', onSubmit);
              },
            };
          }
          export async function applyAction() {}
          export function deserialize(result) {
            return JSON.parse(result);
          }
        `;
      }
      if (id === virtualNavId) {
        return `
          import { setHarnessPage } from '$app/state';
          export function pushState(url) {
            const resolved = new URL(url, window.location.href);
            window.history.replaceState({}, '', resolved);
            setHarnessPage({ href: resolved.href });
          }
          export async function invalidateAll() {}
        `;
      }
      if (id === virtualEntryId) {
        return `
          import { flushSync, mount, unmount } from 'svelte';
          import { setHarnessPage } from '$app/state';
          import '${join(ROUTES_DIR, "layout.css")}';
          import HomePage from '${join(ROUTES_DIR, "+page.svelte")}';
          import LoginPage from '${join(ROUTES_DIR, "login/+page.svelte")}';
          import AccountPage from '${join(ROUTES_DIR, "account/+page.svelte")}';
          import AdminPage from '${join(ROUTES_DIR, "admin/+page.svelte")}';
          import ErrorPage from '${join(ROUTES_DIR, "+error.svelte")}';

          const routes = {
            home: HomePage,
            login: LoginPage,
            account: AccountPage,
            admin: AdminPage,
            error: ErrorPage,
          };

          let currentInstance = null;

          window.__harness = {
            mountRoute(routeName, props = {}, pagePatch = {}) {
              const target = document.getElementById('app');
              if (currentInstance) {
                unmount(currentInstance);
                currentInstance = null;
              }
              target.innerHTML = '';
              setHarnessPage({
                href: pagePatch.href ?? 'http://127.0.0.1/',
                status: pagePatch.status ?? 404,
                error: pagePatch.error ?? { message: 'Not found' },
              });
              const Component = routes[routeName];
              flushSync(() => {
                currentInstance = mount(Component, { target, props });
              });
              return true;
            },
            setPage(patch) {
              flushSync(() => {
                setHarnessPage(patch);
              });
            },
          };
        `;
      }
      return null;
    },
  };

  const result = await build({
    root: PROJECT_ROOT,
    configFile: false,
    logLevel: "silent",
    plugins: [
      virtualKitPlugin,
      tailwindcss(),
      svelte({
        compilerOptions: {
          runes: ({ filename }) =>
            filename.split(/[/\\]/).includes("node_modules") ? undefined : true,
        },
      }),
    ],
    build: {
      write: false,
      target: "esnext",
      minify: false,
      cssCodeSplit: false,
      rollupOptions: {
        input: "virtual:reflow-harness-entry.ts",
        output: {
          entryFileNames: "assets/entry.js",
          chunkFileNames: "assets/[name]-[hash].js",
          assetFileNames: "assets/[name][extname]",
        },
      },
    },
  });

  const outputs = Array.isArray(result) ? result : [result];
  const files = new Map<string, { body: string | Uint8Array; contentType: string }>();
  const cssLinks: string[] = [];

  for (const bundle of outputs) {
    if (!("output" in bundle)) continue;
    for (const item of bundle.output) {
      const urlPath = `/${item.fileName}`;
      if (item.type === "chunk") {
        files.set(urlPath, {
          body: item.code,
          contentType: "text/javascript; charset=utf-8",
        });
      } else {
        const contentType = item.fileName.endsWith(".css")
          ? "text/css; charset=utf-8"
          : item.fileName.endsWith(".svg")
            ? "image/svg+xml"
            : "application/octet-stream";
        files.set(urlPath, {
          body: item.source,
          contentType,
        });
        if (item.fileName.endsWith(".css")) {
          cssLinks.push(`<link rel="stylesheet" href="${urlPath}" />`);
        }
      }
    }
  }

  const indexHtml = `<!doctype html>
<html lang="en" data-theme="cerberus" data-mode="dark" data-color-scheme="dark" class="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#121212" />
  ${cssLinks.join("\n  ")}
  <style id="mode-override"></style>
</head>
<body class="m-0 bg-surface-50-950 text-surface-900-100">
  <div id="cascade-probe" hidden>
    <button id="plain-utility-btn" class="btn h-10 text-left whitespace-nowrap">Plain utility button</button>
    <button id="reflow-btn" class="btn btn-reflow w-36">Save Librarr connection settings</button>
    <button id="reflow-overridden-btn" class="btn btn-reflow h-12 text-left whitespace-nowrap">Utility wins over layer</button>
  </div>
  <div id="app"></div>
  <script type="module" src="/assets/entry.js"></script>
</body>
</html>`;

  files.set("/", {
    body: indexHtml,
    contentType: "text/html; charset=utf-8",
  });

  return { files };
}

async function withHarnessServer<T>(
  assets: HarnessAssets,
  fn: (baseUrl: string) => Promise<T>,
): Promise<T> {
  const librarrResultsPayload = {
    results: Array.from({ length: 8 }, (_, idx) => ({
      id: `lib-${idx + 1}`,
      title: `The Ministry for the Future: Unabridged Annotated Edition Volume ${idx + 1}`,
      author: "Kim Stanley Robinson · Narrated by Jennifer Vuletic and Full Ensemble Cast",
      format: "audiobook",
      score: idx < 2 ? 95 - idx : 82 - idx,
      cover_url: "",
    })),
  };

  const server = http.createServer((req, res) => {
    const reqUrl = new URL(req.url ?? "/", "http://127.0.0.1");
    if (reqUrl.pathname === "/api/librarr/search/stream") {
      res.writeHead(200, { "content-type": "text/event-stream; charset=utf-8" });
      res.end(
        `event: results\ndata: ${JSON.stringify(librarrResultsPayload)}\n\nevent: complete\ndata: {}\n\n`,
      );
      return;
    }
    if (reqUrl.pathname === "/api/books/read") {
      res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      res.end(
        JSON.stringify({
          items: Array.from({ length: 35 }, (_, idx) => ({
            id: idx + 1,
            title: `Ancillary Justice Collector Archive Volume ${idx + 1}`,
            author: "Ann Leckie",
            rating: (idx % 5) + 1,
            read_at: "2026-02-14",
            source: "goodreads_csv",
            created_at: "2026-02-15T00:00:00Z",
            rank_score: 88.5,
            algorithm_score: null,
            algorithm_explanation: [],
            metadata_confidence: 0.92,
          })),
        }),
      );
      return;
    }
    if (reqUrl.pathname === "/api/books/read/scores") {
      res.writeHead(503, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ detail: "Temporary score service interruption." }));
      return;
    }
    if (reqUrl.pathname.startsWith("/api/telemetry")) {
      res.writeHead(204);
      res.end();
      return;
    }
    const asset = assets.files.get(reqUrl.pathname);
    if (!asset) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    res.writeHead(200, { "content-type": asset.contentType });
    res.end(asset.body);
  });

  const port = await new Promise<number>((resolveListen, rejectListen) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      resolveListen(typeof addr === "object" && addr ? addr.port : 0);
    });
    server.on("error", rejectListen);
  });

  try {
    return await fn(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
  }
}

async function withHeadlessChrome<T>(fn: (cdp: CdpClient) => Promise<T>): Promise<T> {
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
        ...(binary.includes("chrome-headless-shell") ? [] : ["--headless=new"]),
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--enable-features=NetworkService,NetworkServiceInProcess",
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

function createHomeFixtureData() {
  const discoverBook = {
    id: 101,
    title: "The Long Way to a Small, Angry Planet (Wayfarers Special Archive Edition)",
    author: "Becky Chambers",
    synopsis: "A crew tunnels wormholes across deep space.",
    cover_url: "",
    source_url: "https://books.example.org/wayfarers/small-angry-planet",
    published_on: "2026-03-01",
    published_kind: "published",
    genres: ["Science Fiction", "Space Opera"],
    score: 94,
    metadata_confidence: 0.95,
    reason: "Matches your high ratings for character-driven speculative fiction.",
    status: "recommended",
    source_name: "Locus Recommended Reading",
    recommendation_run_id: "run-1",
  };
  const savedBook = {
    ...discoverBook,
    id: 102,
    title: "A Memory Called Empire (Teixcalaan Annotated Collector Volume)",
    author: "Arkady Martine",
    status: "saved",
  };
  const decisionBook = {
    ...discoverBook,
    id: 103,
    title: "The City We Became",
    author: "N. K. Jemisin",
    status: "maybe_later",
  };

  return {
    user: {
      id: "u-1",
      username: "alexandra-montgomery-archivist",
      display_name: "Alexandra Montgomery-Archivist",
      role: "admin",
      profile_id: "p-1",
    },
    recommendation_run_id: "run-1",
    books: [discoverBook, savedBook],
    recommendations_has_more: true,
    recommendation_genres: ["Science Fiction", "Space Opera"],
    decisions: [decisionBook],
    decisions_has_more: true,
    decision_genres: ["Science Fiction"],
    history: [
      {
        id: 1,
        title: "Piranesi",
        author: "Susanna Clarke",
        rating: 5,
        read_at: "2026-01-10",
      },
    ],
    readCount: 35,
    backupStatus: {
      enabled: true,
      interval_hours: 24,
      retention_count: 7,
      last_success_at: "2026-11-24T02:00:00Z",
      last_error: null,
      next_backup_at: "2026-11-25T02:00:00Z",
      backups: [
        {
          id: "bookward-20261124T020000Z-a1b2c3d4",
          created_at: "2026-11-24T02:00:00Z",
          size_bytes: 524288,
        },
      ],
    },
    digestReview: {
      requested: true,
      ids: [101],
      books: [discoverBook],
    },
    sources: [
      {
        id: 501,
        name: "Tor Speculative Releases Feed",
        label: "Tor Speculative Releases Feed",
        url: "https://books.example.org/feeds/extremely-long-unbroken-catalog-identifier-segment-2026/rss.xml",
        enabled: 1,
        kind: "rss",
        is_default: 0,
        lifecycle: "permanent" as const,
        filters: { include_genres: ["science fiction"], exclude_genres: [] },
        last_status: "ok",
        last_scanned_at: "2026-11-24T01:00:00Z",
      },
    ],
    profile: {
      goodreads_url: "https://www.goodreads.com/review/list_rss/12345678?shelf=read",
      goodreads_rss_last_sync_at: "2026-11-24T02:00:00Z",
      goodreads_rss_last_status: "ok",
      goodreads_rss_last_attempt_at: "2026-11-24T02:00:00Z",
      goodreads_rss_last_error: "",
      default_source_enabled: 1,
      librar_url: "https://librarr.example.org",
      librar_connected: 1,
      embedding_backend: "openai",
      embedding_model: "text-embedding-3-small",
      embedding_url: "https://api.openai.example.org/v1",
      embedding_api_key_set: true,
      librarr_url: "https://librarr.example.org",
      librarr_api_key_set: true,
      nyt_api_key_set: true,
      google_books_api_key_set: true,
      librarr_media_type: "audiobook" as const,
      source_sync_interval_hours: 24,
      llm_rerank: {
        enabled: true,
        provider: "openai_compatible",
        api_url: "https://llm.example.org/v1",
        model: "gpt-4o-mini",
        api_key_set: true,
      },
      digest: {
        enabled: true,
        channels: ["discord", "email"] as Array<"discord" | "email">,
        day: 1,
        time: "08:00",
        timezone: "America/Phoenix",
        minimum_score: 75,
        maximum_books: 5,
        only_new: true,
        app_url: "https://bookward.example.org",
        discord_webhook_set: true,
        email_to: "reader@example.org",
        email_from: "bookward@example.org",
        smtp_host: "smtp.example.org",
        smtp_port: 587,
        smtp_security: "starttls" as const,
        smtp_username_set: true,
        smtp_password_set: true,
        last_delivery: {
          id: 9,
          channel: "email",
          status: "failed",
          sent_at: null,
          updated_at: "2026-11-24T08:00:00Z",
          error: "SMTP connection timed out.",
        },
      },
      api_tokens: [
        {
          id: 71,
          name: "Home Assistant Reading Automation Token",
          token_prefix: "bkw_live_98ab",
          created_at: "2026-11-01",
          last_used_at: "2026-11-23",
          revoked_at: null,
        },
      ],
    },
  };
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
      "text-reflow",
      "url-reflow",
      "btn-reflow",
      "btn",
      "badge",
      "h-12",
      "text-left",
      "whitespace-nowrap",
    ]);

    expect(compiledCss).toMatch(/@layer\s+[^;]*components\s*,\s*utilities\s*;/);
    expect(compiledCss).toContain("@layer components");
    // Only #274's narrow <= 24rem media block may target .btn:not(.btn-icon); no viewport-wide .btn or .badge overrides
    const withoutNarrowBtnMedia = compiledCss.replace(
      /@media\s*\(max-width:\s*24rem\)\s*\{[\s\S]*?\n\}/g,
      "",
    );
    expect(withoutNarrowBtnMedia).not.toContain(".btn:not(.btn-icon)");
    expect(compiledCss).not.toContain(".badge:not(");

    const btnIdx = compiledCss.indexOf(".btn {");
    const btnReflowIdx = compiledCss.indexOf(".btn-reflow {");
    const whitespaceNowrapIdx = compiledCss.indexOf(".whitespace-nowrap {");
    expect(btnIdx).toBeGreaterThan(-1);
    expect(btnReflowIdx).toBeGreaterThan(btnIdx);
    expect(whitespaceNowrapIdx).toBeGreaterThan(btnReflowIdx);
  });

  it("renders ThemePicker SSR triggers with PopoverDialog topLayer semantics and single-scroller structure", () => {
    const compactRender = render(ThemePicker, { props: { compact: true } });
    expect(compactRender.body).toContain('aria-label="Choose appearance"');
    expect(compactRender.body).toMatch(/aria-controls="[^"]+"/);
    expect(compactRender.body).toContain('aria-haspopup="dialog"');
    expect(compactRender.body).toContain("fixed right-4 top-4 left-auto bottom-auto z-[55]");
    expect(compactRender.body).toContain("sm:top-20");
    expect(compactRender.body).toContain("btn-reflow");
    expect(compactRender.body).toContain(">Appearance</span>");
    expect(compactRender.body).toContain("overflow-y-auto");
    expect(compactRender.body).not.toContain("max-h-[min(22rem,45vh)]");

    const standaloneRender = render(ThemePicker, { props: { compact: false } });
    expect(standaloneRender.body).toContain("btn-icon");
    expect(standaloneRender.body).toContain("absolute right-0 top-[calc(100%+0.75rem)] z-50");
    expect(standaloneRender.body).not.toContain(">Appearance</span>");
    expect(standaloneRender.body).not.toContain("max-h-[min(22rem,45vh)]");
  });

  const chromeIt = findChromeBinary() ? it : it.skip;

  chromeIt(
    "mounts the real Svelte route and ModalDialog/PopoverDialog components in Chromium to verify compact ThemePicker anchoring, 320px sticky nav flow, single-scroll ModalDialog trapping, and .btn reflow across routes",
    async () => {
      const assets = await buildRealComponentHarness();
      const homeData = createHomeFixtureData();
      const homeForm = {
        zeroMatchConfirm: {
          id: 501,
          url: "https://books.example.org/feeds/rss.xml",
          rawCount: 14,
          includeGenres: "cyberpunk",
          excludeGenres: "",
        },
      };

      await withHarnessServer(assets, async (baseUrl) => {
        await withHeadlessChrome(async (cdp) => {
          await cdp.send("Emulation.setDeviceMetricsOverride", {
            width: 1280,
            height: 800,
            deviceScaleFactor: 2,
            mobile: false,
          });
          await cdp.send("Page.navigate", { url: baseUrl });

          // Wait for the real Vite client bundle to register window.__harness
          for (let attempt = 0; attempt < 80; attempt++) {
            const ready = await cdp.eval<boolean>(
              "typeof window.__harness?.mountRoute === 'function'",
            );
            if (ready) break;
            await new Promise((r) => setTimeout(r, 50));
          }

          // 1. Verify Tailwind utility cascade wins on plain .btn AND .btn-reflow above 24rem
          const cascadeMetrics = await cdp.eval<{
            plainHeight: string;
            plainAlign: string;
            plainWrap: string;
            reflowWraps: boolean;
            overrideHeight: string;
            overrideAlign: string;
            overrideWrap: string;
          }>(`(() => {
            const probe = document.getElementById('cascade-probe');
            probe.hidden = false;
            const plain = getComputedStyle(document.getElementById('plain-utility-btn'));
            const reflowEl = document.getElementById('reflow-btn');
            const overridden = getComputedStyle(document.getElementById('reflow-overridden-btn'));
            const out = {
              plainHeight: plain.height,
              plainAlign: plain.textAlign,
              plainWrap: plain.whiteSpace,
              reflowWraps: reflowEl.scrollWidth <= reflowEl.clientWidth + 1 && reflowEl.clientHeight > 40,
              overrideHeight: overridden.height,
              overrideAlign: overridden.textAlign,
              overrideWrap: overridden.whiteSpace,
            };
            probe.hidden = true;
            return out;
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

          await cdp.send("Emulation.setDeviceMetricsOverride", {
            width: 375,
            height: 812,
            deviceScaleFactor: 2,
            mobile: false,
          });

          // Mount the REAL +page.svelte component!
          await cdp.eval(
            `window.__harness.mountRoute('home', { data: ${JSON.stringify(homeData)}, form: ${JSON.stringify(homeForm)} }, { href: '${baseUrl}/?digest=1' })`,
          );

          // 2. Open the REAL PopoverDialog profile menu and REAL compact ThemePicker at 375x812 (100% and 200%)
          await cdp.eval(`(() => {
            document.querySelector('button[aria-label="Open profile menu"]').click();
            document.querySelector('#profile-menu-panel button[aria-label="Choose appearance"]').click();
          })()`);

          for (const cssMode of ["", "html { font-size: 200% !important; }"]) {
            await cdp.eval(
              `document.getElementById('mode-override').textContent = ${JSON.stringify(cssMode)}`,
            );
            // Trigger resize so ThemePicker's real $effect syncBounds listener updates inline top/maxHeight
            await cdp.eval("window.dispatchEvent(new Event('resize'))");
            await new Promise((r) => setTimeout(r, 30));

            const pickerMetrics = await cdp.eval<{
              isOpen: boolean;
              clearsHeader: boolean;
              overlapsProfileTrigger: boolean;
              panelBottom: number;
              panelRight: number;
              panelLeft: number;
              nestedScrollCount: number;
              clippedLabels: number;
            }>(`(() => {
              const trigger = document.querySelector('#profile-menu-panel button[aria-label="Choose appearance"]');
              const panelId = trigger.getAttribute('aria-controls');
              const panel = document.getElementById(panelId);
              const profileBtn = document.querySelector('button[aria-label="Open profile menu"]');
              const header = document.querySelector('header');
              const pr = panel.getBoundingClientRect();
              const tr = profileBtn.getBoundingClientRect();
              const hr = header.getBoundingClientRect();
              const xOverlap = Math.max(0, Math.min(pr.right, tr.right) - Math.max(pr.left, tr.left));
              const yOverlap = Math.max(0, Math.min(pr.bottom, tr.bottom) - Math.max(pr.top, tr.top));
              const nestedScrollCount = Array.from(panel.querySelectorAll('[role="radiogroup"]')).filter(
                (el) => getComputedStyle(el).overflowY === 'auto' || getComputedStyle(el).overflowY === 'scroll',
              ).length;
              const labels = Array.from(panel.querySelectorAll('.text-reflow'));
              const clippedLabels = labels.filter((s) => s.scrollWidth > s.clientWidth + 1.5).length;
              return {
                isOpen: !panel.hidden && trigger.getAttribute('aria-expanded') === 'true',
                clearsHeader: pr.top >= hr.bottom + 8,
                overlapsProfileTrigger: xOverlap > 1 && yOverlap > 1,
                panelBottom: pr.bottom,
                panelRight: pr.right,
                panelLeft: pr.left,
                nestedScrollCount,
                clippedLabels,
              };
            })()`);

            expect(pickerMetrics.isOpen).toBe(true);
            expect(pickerMetrics.clearsHeader).toBe(true);
            expect(pickerMetrics.overlapsProfileTrigger).toBe(false);
            expect(pickerMetrics.panelBottom).toBeLessThanOrEqual(812 + 1.5);
            expect(pickerMetrics.panelRight).toBeLessThanOrEqual(375 + 1.5);
            expect(pickerMetrics.panelLeft).toBeGreaterThanOrEqual(-1.5);
            expect(pickerMetrics.nestedScrollCount).toBe(0);
            expect(pickerMetrics.clippedLabels).toBe(0);
          }

          // Close the real compact ThemePicker & profile PopoverDialog
          await cdp.eval(`(() => {
            document.querySelector('button[aria-label="Close appearance picker"]').click();
            document.querySelector('button[aria-label="Open profile menu"]').click();
          })()`);

          // 3. Verify REAL +page.svelte 320x568 mobile nav stays 1 row at 100% & wraps without covering main content at 200%
          await cdp.send("Emulation.setDeviceMetricsOverride", {
            width: 320,
            height: 568,
            deviceScaleFactor: 2,
            mobile: false,
          });
          await cdp.eval(`document.getElementById('mode-override').textContent = ''`);
          const nav320Baseline = await cdp.eval<{
            rows: number;
            overflowPx: number;
            coversMainBottom: boolean;
          }>(`(() => {
            const nav = document.querySelector('nav[aria-label="Mobile navigation"]');
            const buttons = Array.from(nav.querySelectorAll('button'));
            const rows = new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size;
            window.scrollTo(0, document.documentElement.scrollHeight);
            const mainRect = document.getElementById('main-content').getBoundingClientRect();
            const navRect = nav.getBoundingClientRect();
            return {
              rows,
              overflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
              coversMainBottom: mainRect.bottom > navRect.top + 1,
            };
          })()`);
          expect(nav320Baseline).toEqual({
            rows: 1,
            overflowPx: 0,
            coversMainBottom: false,
          });

          await cdp.eval(
            `document.getElementById('mode-override').textContent = 'html { font-size: 200% !important; }'`,
          );
          const nav320Zoomed = await cdp.eval<{
            rows: number;
            overflowPx: number;
            coversMainBottom: boolean;
          }>(`(() => {
            const nav = document.querySelector('nav[aria-label="Mobile navigation"]');
            const buttons = Array.from(nav.querySelectorAll('button'));
            const rows = new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size;
            window.scrollTo(0, document.documentElement.scrollHeight);
            const mainRect = document.getElementById('main-content').getBoundingClientRect();
            const navRect = nav.getBoundingClientRect();
            return {
              rows,
              overflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
              coversMainBottom: mainRect.bottom > navRect.top + 1,
            };
          })()`);
          expect(nav320Zoomed.rows).toBeGreaterThanOrEqual(2);
          expect(nav320Zoomed.overflowPx).toBeLessThanOrEqual(1);
          expect(nav320Zoomed.coversMainBottom).toBe(false);

          // 4. Audit EVERY visible .btn across REAL views and routes at 320x568 with 200% font size + WCAG 1.4.12 text spacing
          const zoomAndSpacingCss =
            "html { font-size: 200% !important; } * { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; }";
          await cdp.eval(
            `document.getElementById('mode-override').textContent = ${JSON.stringify(zoomAndSpacingCss)}`,
          );

          const checkVisibleButtons = async (
            surfaceLabel: string,
            { checkPageOverflow = true }: { checkPageOverflow?: boolean } = {},
          ) => {
            const report = await cdp.eval<{
              clippedButtons: string[];
              pageOverflowPx: number;
            }>(`(() => {
              // Open closed <details> disclosures so nested action buttons (source filters, revoke, unlink) are measured too
              for (const details of document.querySelectorAll('details')) {
                details.open = true;
              }
              const clippedButtons = [];
              for (const btn of document.querySelectorAll('.btn')) {
                const rects = btn.getClientRects();
                if (!rects.length) continue;
                const cs = getComputedStyle(btn);
                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                if (btn.classList.contains('sr-only')) continue;
                if (btn.scrollWidth > btn.clientWidth + 2) {
                  clippedButtons.push((btn.textContent || btn.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' '));
                }
              }
              return {
                clippedButtons,
                pageOverflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
              };
            })()`);
            expect(report.clippedButtons, `${surfaceLabel} clipped buttons`).toEqual([]);
            if (checkPageOverflow) {
              expect(report.pageOverflowPx, `${surfaceLabel} page overflow`).toBeLessThanOrEqual(1.5);
            }
          };

          // Select a digest pick so "Add 1 to shortlist" renders in Discover
          await cdp.eval(`(() => {
            const cb = document.querySelector('input[type="checkbox"][aria-label*="for shortlist"]');
            if (cb && !cb.checked) cb.click();
          })()`);
          await checkVisibleButtons("home:discover");

          for (const navLabel of ["Saved", "History", "Sources", "Settings"]) {
            await cdp.eval(`(() => {
              const btn = Array.from(document.querySelectorAll('nav[aria-label="Mobile navigation"] button'))
                .find((b) => b.textContent.includes(${JSON.stringify(navLabel)}));
              btn.click();
            })()`);
            await new Promise((r) => setTimeout(r, 40));
            await checkVisibleButtons(`home:${navLabel.toLowerCase()}`);
          }

          // Switch History to Read filter so async read history + "Retry scoring" + "Load more books" render
          await cdp.eval(`(() => {
            const historyBtn = Array.from(document.querySelectorAll('nav[aria-label="Mobile navigation"] button'))
              .find((b) => b.textContent.includes('History'));
            historyBtn.click();
          })()`);
          await new Promise((r) => setTimeout(r, 30));
          await cdp.eval(`(() => {
            const readBtn = Array.from(document.querySelectorAll('button'))
              .find((b) => (b.textContent || '').trim().startsWith('Read ('));
            readBtn?.click();
          })()`);
          await new Promise((r) => setTimeout(r, 80));
          await checkVisibleButtons("home:history-read");

          // Mount REAL login (setup & OIDC), account, admin, and +error routes and verify 0 clipped .btn at 320px 200% + 1.4.12
          await cdp.eval(
            `window.__harness.mountRoute('login', { data: { setup_required: true, oidc_enabled: false, next: '/', error: '' }, form: null })`,
          );
          await checkVisibleButtons("login:setup");

          await cdp.eval(
            `window.__harness.mountRoute('login', { data: { setup_required: false, oidc_enabled: true, next: '/', error: '' }, form: null })`,
          );
          await checkVisibleButtons("login:oidc");

          await cdp.eval(
            `window.__harness.mountRoute('account', {
              data: {
                user: { id: 'u-1', username: 'alexandra', display_name: 'Alexandra', role: 'admin', profile_id: 'p-1' },
                forcePasswordChange: false,
                reauthenticationRequired: false,
                linkError: false,
                identityLinked: false,
                oidcEnabled: true,
                methods: {
                  password_enabled: true,
                  oidc_identities: [{ id: 1, issuer: 'https://auth.example.org/application/o/bookward/', email: 'alexandra@example.org' }],
                },
              },
              form: null,
            })`,
          );
          await checkVisibleButtons("account", { checkPageOverflow: false });

          await cdp.eval(
            `window.__harness.mountRoute('admin', {
              data: {
                user: { id: 'u-1', username: 'alexandra', display_name: 'Alexandra', role: 'admin', profile_id: 'p-1' },
                current_user_id: 'u-1',
                accounts: [
                  { id: 'u-1', username: 'alexandra', display_name: 'Alexandra', role: 'admin', status: 'active' },
                  { id: 'u-2', username: 'marcus-reader', display_name: 'Marcus Reader', role: 'user', status: 'active' },
                ],
                identityBackup: null,
              },
              form: null,
            })`,
          );
          await checkVisibleButtons("admin", { checkPageOverflow: false });

          await cdp.eval(
            `window.__harness.mountRoute('error', {}, { status: 404, error: { message: 'Not found' } })`,
          );
          await checkVisibleButtons("error:404", { checkPageOverflow: false });

          // 5. Remount REAL +page.svelte and verify ModalDialog (Mark Read & Librarr) reflow + single-scroll + focus trapping
          await cdp.eval(
            `window.__harness.mountRoute('home', { data: ${JSON.stringify(homeData)}, form: null }, { href: '${baseUrl}/?view=saved' })`,
          );

          // Verify Mark Read ModalDialog at 320x568 under 200% font size + WCAG 1.4.12 text spacing
          await cdp.eval(
            `document.getElementById('mode-override').textContent = ${JSON.stringify(zoomAndSpacingCss)}`,
          );
          await cdp.eval(`(() => {
            const markBtn = document.querySelector('button[aria-label^="Mark Read:"]');
            markBtn.focus();
            markBtn.click();
          })()`);
          const markReadModalMetrics = await cdp.eval<{
            isModalTopLayer: boolean;
            clippedTextCount: number;
            clippedButtonCount: number;
            restoredFocusStartsWithMarkRead: boolean;
          }>(`(async () => {
            const dialog = document.querySelector('dialog[open][aria-labelledby="mark-read-title"]');
            const isModalTopLayer = Boolean(dialog && dialog.matches(':modal'));
            const clippedTextCount = Array.from(dialog.querySelectorAll('.text-reflow')).filter(
              (el) => el.scrollWidth > el.clientWidth + 2,
            ).length;
            const clippedButtonCount = Array.from(dialog.querySelectorAll('.btn')).filter(
              (btn) => btn.getClientRects().length > 0 && btn.scrollWidth > btn.clientWidth + 2,
            ).length;
            dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
            for (let i = 0; i < 15 && document.querySelector('dialog[open][aria-labelledby="mark-read-title"]'); i++) {
              await new Promise((r) => setTimeout(r, 20));
            }
            return {
              isModalTopLayer,
              clippedTextCount,
              clippedButtonCount,
              restoredFocusStartsWithMarkRead: (document.activeElement?.getAttribute('aria-label') ?? '').startsWith('Mark Read:'),
            };
          })()`);
          expect(markReadModalMetrics).toEqual({
            isModalTopLayer: true,
            clippedTextCount: 0,
            clippedButtonCount: 0,
            restoredFocusStartsWithMarkRead: true,
          });

          // Switch back to Discover and verify Librarr ModalDialog at 320x568 + 200% font size has NO nested header scroll and Close stays pinned
          await cdp.eval(
            `window.__harness.mountRoute('home', { data: ${JSON.stringify(homeData)}, form: null }, { href: '${baseUrl}/' })`,
          );
          await cdp.eval(
            `document.getElementById('mode-override').textContent = 'html { font-size: 200% !important; }'`,
          );
          await cdp.eval(`(() => {
            document.body.style.overflow = 'auto';
            const findBtn = document.querySelector('button[aria-label^="Find in Librarr:"]');
            findBtn.focus();
            findBtn.click();
          })()`);

          for (let attempt = 0; attempt < 60; attempt++) {
            const count = await cdp.eval<number>(
              "document.querySelectorAll('dialog[open][aria-labelledby=\"librarr-search-title\"] article').length",
            );
            if (count === 8) break;
            await new Promise((r) => setTimeout(r, 30));
          }

          const mobileLibrarrHeader = await cdp.eval<{
            isModalTopLayer: boolean;
            headerOverflowY: string;
            dialogOverflowY: string;
            bodyOverflowY: string;
            closeVisible: boolean;
            bulkButtonClipped: boolean;
          }>(`(() => {
            const dialog = document.querySelector('dialog[open][aria-labelledby="librarr-search-title"]');
            const [header, body] = Array.from(dialog.children);
            const closeBtn = dialog.querySelector('button[aria-label="Close Librarr search"]');
            const bulkBtn = Array.from(body.querySelectorAll('button')).find((b) =>
              (b.textContent || '').includes('Add high-confidence matches'),
            );
            const dr = dialog.getBoundingClientRect();
            const cr = closeBtn.getBoundingClientRect();
            return {
              isModalTopLayer: dialog.matches(':modal'),
              headerOverflowY: getComputedStyle(header).overflowY,
              dialogOverflowY: getComputedStyle(dialog).overflowY,
              bodyOverflowY: getComputedStyle(body).overflowY,
              closeVisible: cr.top >= dr.top - 1 && cr.bottom <= dr.bottom + 1 && cr.right <= dr.right + 1,
              bulkButtonClipped: bulkBtn ? bulkBtn.scrollWidth > bulkBtn.clientWidth + 2 : true,
            };
          })()`);
          expect(mobileLibrarrHeader).toEqual({
            isModalTopLayer: true,
            headerOverflowY: "visible",
            dialogOverflowY: "hidden",
            bodyOverflowY: "auto",
            closeVisible: true,
            bulkButtonClipped: false,
          });

          // Switch to 1280x800 + WCAG 1.4.12 text spacing to verify desktop header wrapping, Librarr ModalDialog scroll bounds, and REAL modalFocus Tab/Shift+Tab/destroy
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
            lockedBodyOverflow: string;
            clipPx: number;
            lastAddExposed: boolean;
            initialFocusLabel: string;
            wrappedForwardLabel: string;
            wrappedBackwardIsLastAdd: boolean;
            restoredOverflow: string;
            restoredFocusStartsWithFind: boolean;
          }>(`(async () => {
            const navBtns = Array.from(document.querySelectorAll('nav[aria-label="Primary navigation"] button'));
            const actBtns = [
              document.querySelector('button[aria-label="Refresh recommendations"]'),
              document.querySelector('button[aria-label="Open profile menu"]'),
            ];
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

            const dialog = document.querySelector('dialog[open][aria-labelledby="librarr-search-title"]');
            const lockedBodyOverflow = document.body.style.overflow;
            const initialFocusLabel = document.activeElement?.getAttribute('aria-label') ?? '';

            const [, body] = Array.from(dialog.children);
            const dr = dialog.getBoundingClientRect();
            const br = body.getBoundingClientRect();
            const clipPx = Math.max(0, br.bottom - dr.bottom);
            body.scrollTop = body.scrollHeight;
            const addBtns = Array.from(body.querySelectorAll('article button'));
            const lastAdd = addBtns[addBtns.length - 1];
            const lr = lastAdd.getBoundingClientRect();
            const lastAddExposed = lr.bottom <= br.bottom + 1.5 && lr.top >= br.top - 1.5;

            const closeBtn = dialog.querySelector('button[aria-label="Close Librarr search"]');

            // Exercise REAL ModalDialog / modalFocus keydown listener attached to <dialog>
            lastAdd.focus();
            dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
            const wrappedForwardLabel = document.activeElement?.getAttribute('aria-label') ?? '';

            closeBtn.focus();
            dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
            const wrappedBackwardIsLastAdd = document.activeElement === lastAdd;

            // Close the real ModalDialog and verify synchronous focusTrap/showModal cleanup
            closeBtn.click();
            for (let i = 0; i < 15 && document.querySelector('dialog[open][aria-labelledby="librarr-search-title"]'); i++) {
              await new Promise((r) => setTimeout(r, 20));
            }

            return {
              headerOverlapCount,
              lockedBodyOverflow,
              clipPx: Math.round(clipPx * 10) / 10,
              lastAddExposed,
              initialFocusLabel,
              wrappedForwardLabel,
              wrappedBackwardIsLastAdd,
              restoredOverflow: document.body.style.overflow,
              restoredFocusStartsWithFind: (document.activeElement?.getAttribute('aria-label') ?? '').startsWith('Find in Librarr:'),
            };
          })()`);

          expect(modalAndFocusMetrics).toEqual({
            headerOverlapCount: 0,
            lockedBodyOverflow: "hidden",
            clipPx: 0,
            lastAddExposed: true,
            initialFocusLabel: "Search Librarr by title or author",
            wrappedForwardLabel: "Close Librarr search",
            wrappedBackwardIsLastAdd: true,
            restoredOverflow: "auto",
            restoredFocusStartsWithFind: true,
          });
        });
      });
    },
    45_000,
  );
});

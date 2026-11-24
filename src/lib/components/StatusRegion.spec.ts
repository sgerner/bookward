import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { compile, parse } from "svelte/compiler";
import { render } from "svelte/server";
import { LIVE_STATUS_DEBOUNCE_MS, liveStatus } from "../live-status";
import StatusRegion from "./StatusRegion.svelte";

type AstNode = {
  type?: string;
  name?: string;
  start?: number;
  end?: number;
  attributes?: Array<{
    type: string;
    name?: string;
    start?: number;
    end?: number;
    value?: true | Array<{ type?: string; data?: string }>;
  }>;
  [key: string]: unknown;
};

function readPageAst() {
  const source = readFileSync(
    fileURLToPath(new URL("../../routes/+page.svelte", import.meta.url)),
    "utf8",
  );
  const ast = parse(source, { modern: true });
  return { source, fragment: ast.fragment as unknown as AstNode };
}

function collectNodes(
  node: unknown,
  predicate: (candidate: AstNode, ancestors: AstNode[]) => boolean,
  ancestors: AstNode[] = [],
): Array<{ node: AstNode; ancestors: AstNode[] }> {
  if (!node || typeof node !== "object") return [];
  const record = node as AstNode;
  const found: Array<{ node: AstNode; ancestors: AstNode[] }> = [];

  if (predicate(record, ancestors)) {
    found.push({ node: record, ancestors });
  }

  const nextAncestors = record.type ? [...ancestors, record] : ancestors;
  for (const [key, value] of Object.entries(record)) {
    if (key === "attributes") continue;
    if (Array.isArray(value)) {
      for (const child of value) {
        found.push(...collectNodes(child, predicate, nextAncestors));
      }
    } else if (value && typeof value === "object") {
      found.push(...collectNodes(value, predicate, nextAncestors));
    }
  }
  return found;
}

function getAttrRaw(source: string, element: AstNode, attrName: string): string {
  const attr = element.attributes?.find(
    (item) => item.type === "Attribute" && item.name === attrName,
  );
  if (!attr || typeof attr.start !== "number" || typeof attr.end !== "number") {
    return "";
  }
  return source.slice(attr.start, attr.end);
}

describe("StatusRegion SSR markup", () => {
  it("renders a pre-mounted polite status region with suppressed initial mount text", () => {
    const { body } = render(StatusRegion, {
      props: { message: "Showing 8 recommendations." },
    });

    expect(body).toContain('role="status"');
    expect(body).toContain('aria-live="polite"');
    expect(body).toContain('aria-atomic="true"');
    expect(body).toContain('class="sr-only"');
    expect(body).not.toContain("Showing 8 recommendations.");
  });

  it("accepts a custom class while preserving ARIA status semantics", () => {
    const { body } = render(StatusRegion, {
      props: { message: "Copied to clipboard.", class: "text-xs" },
    });

    expect(body).toContain('class="text-xs"');
    expect(body).toContain('role="status"');
  });
});

describe("StatusRegion liveStatus DOM action", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps initial mount silent, debounces filter text updates, and flushes immediate updates to DOM textContent", () => {
    const node = { textContent: "" };
    const action = liveStatus(node, {
      message: "Showing 8 of more recommendations.",
      contextKey: "discover",
    });

    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(node.textContent).toBe("");

    action.update({
      message: "3 recommendations match these filters.",
      contextKey: "discover",
    });
    expect(node.textContent).toBe("");

    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS);
    expect(node.textContent).toBe("3 recommendations match these filters.");

    action.update({
      message: "Showing 16 of more recommendations.",
      contextKey: "discover",
      immediate: true,
    });
    expect(node.textContent).toBe("Showing 16 of more recommendations.");

    action.destroy();
  });

  it("stays silent across view contextKey switches and outgoing paused transitions", () => {
    const node = { textContent: "" };
    const action = liveStatus(node, {
      message: "Showing 16 of more recommendations.",
      contextKey: "discover",
      immediate: true,
    });

    expect(node.textContent).toBe("");

    // Switching tabs changes contextKey while immediate may still be true from prior pagination
    action.update({
      message: "4 books on your shortlist.",
      contextKey: "saved",
      immediate: true,
    });
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(node.textContent).toBe("");

    // Outgoing transition instance with paused=true never mutates live DOM textContent
    action.update({
      message: "No shortlist books match these filters.",
      contextKey: "saved",
      paused: true,
      immediate: true,
    });
    vi.advanceTimersByTime(LIVE_STATUS_DEBOUNCE_MS * 2);
    expect(node.textContent).toBe("");

    action.destroy();
  });

  it("mutates pre-mounted modal StatusRegion text asynchronously when announceOnMount is true", () => {
    const node = { textContent: "" };
    const action = liveStatus(node, {
      message: "Searching Librarr…",
      immediate: true,
      announceOnMount: true,
    });

    expect(node.textContent).toBe("");
    vi.advanceTimersByTime(0);
    expect(node.textContent).toBe("Searching Librarr…");

    action.update({
      message: "Found 2 audiobook results.",
      immediate: true,
      announceOnMount: true,
    });
    expect(node.textContent).toBe("Found 2 audiobook results.");

    // Dynamic debounceMs reconfiguration preserves live updates cleanly
    action.update({
      message: "Updated with custom debounce.",
      debounceMs: 120,
      announceOnMount: false,
    });
    vi.advanceTimersByTime(120);
    expect(node.textContent).toBe("Updated with custom debounce.");

    action.destroy();
  });
});

describe("+page.svelte live region and alert wiring", () => {
  it("pre-mounts recommendation and Reading History StatusRegions outside conditional empty/read branches", () => {
    const { source, fragment } = readPageAst();
    const snippets = collectNodes(
      fragment,
      (node) => node.type === "SnippetBlock",
    );
    const recommendationSnippet = snippets.find(({ node }) =>
      source.slice(node.start ?? 0, (node.start ?? 0) + 80).includes("recommendationView"),
    );
    expect(recommendationSnippet).toBeDefined();

    const statusRegions = collectNodes(
      recommendationSnippet!.node,
      (node) => node.type === "Component" && node.name === "StatusRegion",
    );
    expect(statusRegions).toHaveLength(2);

    const [recStatus, readHistoryStatus] = statusRegions;
    expect(getAttrRaw(source, recStatus.node, "contextKey")).toContain("view");
    expect(getAttrRaw(source, recStatus.node, "paused")).toContain("activeView !== view");
    expect(
      recStatus.ancestors.some((ancestor) => ancestor.type === "IfBlock"),
    ).toBe(false);

    expect(getAttrRaw(source, readHistoryStatus.node, "contextKey")).toContain(
      "decisions-read",
    );
    expect(getAttrRaw(source, readHistoryStatus.node, "paused")).toContain(
      'activeView !== "decisions"',
    );
    const readIfAncestors = readHistoryStatus.ancestors.filter(
      (ancestor) => ancestor.type === "IfBlock",
    );
    // Mounted inside `{#if view === "decisions"}` at snippet root, NOT inside the `{:else}` Read-only branch.
    expect(readIfAncestors).toHaveLength(1);
    expect(
      source.slice(readIfAncestors[0].start ?? 0, (readIfAncestors[0].start ?? 0) + 40),
    ).toContain('view === "decisions"');

    // No raw role="status" or aria-live elements inside recommendationView
    const rawLiveNodes = collectNodes(
      recommendationSnippet!.node,
      (node) =>
        node.type === "RegularElement" &&
        Boolean(
          getAttrRaw(source, node, "role").includes("status") ||
            getAttrRaw(source, node, "aria-live"),
        ),
    );
    expect(rawLiveNodes).toHaveLength(0);
  });

  it("routes Librarr modal status updates through StatusRegion without conditional role=status elements", () => {
    const { source, fragment } = readPageAst();
    const dialogs = collectNodes(
      fragment,
      (node) => node.type === "RegularElement" && node.name === "dialog",
    );
    const librarrDialog = dialogs.find(({ node }) =>
      getAttrRaw(source, node, "aria-labelledby").includes("librarr-search-title"),
    );
    expect(librarrDialog).toBeDefined();

    const modalStatusRegions = collectNodes(
      librarrDialog!.node,
      (node) => node.type === "Component" && node.name === "StatusRegion",
    );
    expect(modalStatusRegions.length).toBeGreaterThanOrEqual(2);

    const rawModalStatuses = collectNodes(
      librarrDialog!.node,
      (node) =>
        node.type === "RegularElement" &&
        Boolean(
          getAttrRaw(source, node, "role").includes("status") ||
            getAttrRaw(source, node, "aria-live"),
        ),
    );
    expect(rawModalStatuses).toHaveLength(0);
  });

  it("keeps revealedApiToken outside all live regions and pre-mounts copy feedback StatusRegion", () => {
    const { source, fragment } = readPageAst();
    const codeElements = collectNodes(
      fragment,
      (node) =>
        node.type === "RegularElement" &&
        node.name === "code" &&
        source.slice(node.start ?? 0, node.end ?? 0).includes("revealedApiToken"),
    );
    expect(codeElements).toHaveLength(1);

    const liveAncestors = codeElements[0].ancestors.filter(
      (ancestor) =>
        (ancestor.type === "RegularElement" &&
          (getAttrRaw(source, ancestor, "role").includes("alert") ||
            getAttrRaw(source, ancestor, "role").includes("status") ||
            Boolean(getAttrRaw(source, ancestor, "aria-live")))) ||
        (ancestor.type === "Component" &&
          (ancestor.name === "StatusRegion" || ancestor.name === "InlineAlert")),
    );
    expect(liveAncestors).toHaveLength(0);

    // Copy status region is pre-mounted outside `{#if revealedApiToken}`
    const copyStatusRegions = collectNodes(
      fragment,
      (node) =>
        node.type === "Component" &&
        node.name === "StatusRegion" &&
        getAttrRaw(source, node, "message").includes("apiTokenCopyStatus"),
    );
    expect(copyStatusRegions).toHaveLength(1);
    const revealedIfAncestors = copyStatusRegions[0].ancestors.filter(
      (ancestor) =>
        ancestor.type === "IfBlock" &&
        source.slice(ancestor.start ?? 0, (ancestor.start ?? 0) + 40).includes("revealedApiToken"),
    );
    expect(revealedIfAncestors).toHaveLength(0);
  });

  it("uses InlineAlert size props rather than conflicting padding or font-size class overrides", () => {
    const { source, fragment } = readPageAst();
    const alerts = collectNodes(
      fragment,
      (node) => node.type === "Component" && node.name === "InlineAlert",
    );
    expect(alerts.length).toBeGreaterThanOrEqual(5);

    for (const { node } of alerts) {
      const classAttr = getAttrRaw(source, node, "class");
      expect(classAttr).not.toMatch(/\bp-\d+\b/);
      expect(classAttr).not.toMatch(/\btext-(xs|sm|base|lg)\b/);
    }
  });

  it("compiles StatusRegion cleanly in client runes mode and leaves zero raw static role=status elements in +page.svelte", () => {
    const componentSource = readFileSync(
      fileURLToPath(new URL("./StatusRegion.svelte", import.meta.url)),
      "utf8",
    );
    const compiled = compile(componentSource, {
      filename: "StatusRegion.svelte",
      generate: "client",
      runes: true,
    });
    expect(compiled.warnings).toEqual([]);
    expect(compiled.js.code).toContain("liveStatus");
    expect(compiled.js.code).toContain("contextKey");
    expect(compiled.js.code).toContain("paused");

    const { source, fragment } = readPageAst();
    const allStatusRegions = collectNodes(
      fragment,
      (node) => node.type === "Component" && node.name === "StatusRegion",
    );
    expect(allStatusRegions.length).toBeGreaterThanOrEqual(9);
    for (const { ancestors } of allStatusRegions) {
      expect(
        ancestors.some(
          (ancestor) => ancestor.type === "RegularElement" && ancestor.name === "details",
        ),
      ).toBe(false);
    }

    const sourcePreviewRegions = allStatusRegions.filter(({ node }) =>
      getAttrRaw(source, node, "message").includes("sourcePreview"),
    );
    expect(sourcePreviewRegions).toHaveLength(1);
    expect(getAttrRaw(source, sourcePreviewRegions[0].node, "paused")).toContain(
      'activeView !== "sources"',
    );
    expect(
      sourcePreviewRegions[0].ancestors.some((ancestor) => ancestor.type === "KeyBlock"),
    ).toBe(false);

    const toastLiveNodes = collectNodes(
      fragment,
      (node) =>
        node.type === "RegularElement" &&
        getAttrRaw(source, node, "role").includes("formIsError"),
    );
    expect(toastLiveNodes).toHaveLength(1);
    expect(
      toastLiveNodes[0].ancestors.some((ancestor) => ancestor.type === "IfBlock"),
    ).toBe(false);

    const rawStatusElements = collectNodes(
      fragment,
      (node) => {
        if (node.type !== "RegularElement") return false;
        const roleAttr = getAttrRaw(source, node, "role");
        const ariaLiveAttr = getAttrRaw(source, node, "aria-live");
        // Only the intentional pre-mounted toast notification uses dynamic `formIsError` role/aria-live.
        if (roleAttr.includes("formIsError") || ariaLiveAttr.includes("formIsError")) {
          return false;
        }
        return roleAttr.includes("status") || Boolean(ariaLiveAttr);
      },
    );
    expect(rawStatusElements).toHaveLength(0);
  });
});

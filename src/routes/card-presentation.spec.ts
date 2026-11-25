import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";

let mockedView = "discover";

vi.mock("$app/state", () => ({
  page: {
    get url() {
      return new URL(`http://bookward.test/?view=${mockedView}`);
    },
  },
}));

vi.mock("$app/navigation", () => ({
  invalidateAll: vi.fn(),
  pushState: vi.fn(),
}));

vi.mock("$app/forms", () => ({
  applyAction: vi.fn(),
  deserialize: vi.fn(),
  enhance: () => () => {},
}));

import Page from "./+page.svelte";

function sampleBook(overrides: Record<string, unknown> = {}) {
  return {
    id: 101,
    recommendation_run_id: "run-abc",
    title: "The Left Hand of Darkness",
    author: "Ursula K. Le Guin",
    synopsis:
      "A lone human emissary is sent to Winter, an icy world whose inhabitants have no fixed gender, where political intrigue and a perilous trek across an ice sheet reshape diplomacy and friendship forever.",
    cover_url: "",
    source_url: "https://books.example/left-hand",
    published_on: "1969-03-01",
    published_kind: "published",
    genres: ["Science fiction", "Speculative"],
    score: 81.24,
    metadata_confidence: 0.95,
    explanation: [
      'Similar to "The Dispossessed" (5★)',
      "Raised using books you shortlisted, added to Librarr, or rated highly (subject Science fiction)",
      "From Curated Sci-Fi",
      "Lowered after appearing on several recent days without a decision",
    ],
    reason: 'Similar to "The Dispossessed" (5★)',
    status: "recommended",
    ...overrides,
  };
}

function basePageData(overrides: Record<string, unknown> = {}) {
  return {
    user: { id: "u1", username: "reader", display_name: "Reader", role: "admin" },
    activeProfileId: "default",
    engineOnline: true,
    recommendation_run_id: "run-abc",
    books: [sampleBook()],
    recommendations_has_more: false,
    recommendation_genres: ["Science fiction", "Speculative"],
    decisions: [
      sampleBook({
        id: 202,
        title: "Passed Book",
        score: 74,
        status: "rejected",
      }),
      sampleBook({
        id: 203,
        title: "Deferred Book",
        score: 88.6,
        status: "maybe_later",
      }),
    ],
    decisions_has_more: false,
    decisionsHasMore: false,
    decision_genres: ["Science fiction", "Speculative"],
    history: [],
    readCount: 24,
    backupStatus: {
      enabled: false,
      interval_hours: 0,
      retention_count: 0,
      last_success_at: null,
      last_error: null,
      next_backup_at: null,
      backups: [],
    },
    digestReview: {
      requested: false,
      ids: [],
      books: [],
    },
    sources: [],
    stats: {
      reads: 24,
      sources: 2,
      saved: 1,
      recommended: 1,
      embeddings: 25,
      librarrConfigured: false,
      goodreadsConfigured: false,
      goodreadsRssUrlSet: false,
      goodreadsLastSyncAt: "",
      goodreadsLastAttemptAt: "",
      goodreadsLastStatus: "",
      goodreadsLastError: "",
      nytApiKeySet: false,
    },
    profile: {
      embedding_backend: "local",
      embedding_model: "hashing-768",
      embedding_url: "",
      embedding_api_key_set: false,
      librarr_url: "",
      librarr_api_key_set: false,
      librarr_media_type: "ebook",
      source_sync_interval_hours: 24,
      api_tokens: [],
      digest: {
        enabled: false,
        channels: [],
        day: 1,
        time: "08:00",
        timezone: "UTC",
        minimum_score: 75,
        maximum_books: 5,
        only_new: true,
        app_url: "",
        discord_webhook_url_set: false,
        discord_webhook_set: false,
        email_to: "",
        email_from: "",
        smtp_host: "",
        smtp_port: 587,
        smtp_username: "",
        smtp_username_set: false,
        smtp_password_set: false,
        smtp_security: "starttls",
        last_sent_at: "",
        last_period: "",
        last_delivery: null,
        deliveries: [],
      },
    },
    ...overrides,
  };
}

describe("recommendation card presentation", () => {
  it("renders inline clamped synopsis, Read full summary toggle, one-decimal Rank badge, and clean explanation list in Discover", () => {
    mockedView = "discover";
    const { body } = render(Page, {
      props: {
        data: basePageData({
          books: [
            sampleBook({ score: 81.24 }),
            sampleBook({
              id: 102,
              title: "The Dispossessed",
              score: 81.25,
            }),
          ],
        }) as never,
        form: null,
      },
    });

    expect(body).toContain("Rank 81.2");
    expect(body).toContain("Rank 81.3");
    expect(body).not.toContain("Rank 81.24");
    expect(body).not.toContain("Rank 81.25");
    expect(body).toContain("A lone human emissary is sent to Winter");
    expect(body).toContain("line-clamp-3");
    expect(body).toContain("Read full summary");
    expect(body).toContain("Why this might be for you");
    expect(body).not.toContain("Book summary");
    expect(body).toMatch(/<ul[^>]*>[\s\S]*<li>Similar to "The Dispossessed" \(5★\)<\/li>/);
    expect(body).toContain(
      "<li>Lowered after appearing on several recent days without a decision</li>",
    );

    // Inline synopsis appears before the collapsible rationale disclosure.
    expect(body.indexOf("A lone human emissary is sent to Winter")).toBeLessThan(
      body.indexOf("Why this might be for you"),
    );
  });

  it("renders status-appropriate rationale headings on Passed and Maybe later cards", () => {
    mockedView = "decisions";
    const { body } = render(Page, {
      props: { data: basePageData() as never, form: null },
    });

    expect(body).toContain("Passed Book");
    expect(body).toContain("Rank 74.0");
    expect(body).toContain("Why Bookward showed this");

    expect(body).toContain("Deferred Book");
    expect(body).toContain("Rank 88.6");
    expect(body).toContain("Why Bookward suggested this");
    expect(body).not.toContain("Book summary");
  });
});

import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";

const { mockPage } = vi.hoisted(() => ({
  mockPage: {
    url: new URL("http://bookward.test/?view=decisions&filter=read"),
    shallow: null as { url: URL } | null,
  },
}));

vi.mock("$app/state", () => ({
  page: mockPage,
}));

vi.mock("$app/navigation", () => ({
  invalidateAll: vi.fn(),
  pushState: vi.fn(),
}));

vi.mock("$app/forms", () => ({
  enhance: vi.fn(),
}));

import Page from "./+page.svelte";

const createPageData = () => ({
  user: {
    user_id: "user-1",
    profile_id: "profile-1",
    username: "reader",
    display_name: "Reader",
    role: "admin",
  },
  recommendation_run_id: "run-1",
  books: [],
  recommendations_has_more: false,
  recommendation_genres: [],
  decisions: [],
  decisions_has_more: false,
  decision_genres: [],
  history: [],
  readCount: 12,
  backupStatus: {
    enabled: false,
    interval_hours: 24,
    retention_count: 14,
    last_success_at: null,
    last_error: null,
    next_backup_at: null,
    backups: [],
  },
  digestReview: { requested: false, ids: [], books: [] },
  sources: [],
  profile: {
    goodreads_url: "",
    goodreads_rss_last_sync_at: "",
    goodreads_rss_last_status: "",
    goodreads_rss_last_attempt_at: "",
    goodreads_rss_last_error: "",
    default_source_enabled: 1,
    librar_url: "",
    librar_connected: 0,
    embedding_backend: "local",
    embedding_model: "local-hash-v1",
    embedding_url: "",
    embedding_api_key_set: false,
    nyt_api_key_set: false,
    librarr_url: "",
    librarr_api_key_set: false,
    librarr_media_type: "audiobook",
    source_sync_interval_hours: 24,
    digest: {
      enabled: false,
      channels: [] as Array<"discord" | "email">,
      day: 5,
      time: "14:00",
      timezone: "UTC",
      minimum_score: 60,
      maximum_books: 5,
      only_new: true,
      app_url: "",
      discord_webhook_set: false,
      email_to: "",
      email_from: "",
      smtp_host: "",
      smtp_port: 587,
      smtp_security: "starttls",
      smtp_username_set: false,
      smtp_password_set: false,
      last_period: "",
      last_delivery: null,
    },
    associations: {
      openlibrary_enabled: false,
      openlibrary_contact: "",
      librarything_enabled: false,
      librarything_api_key_set: false,
      google_books_api_key_set: false,
    },
    llm_rerank: {
      enabled: false,
      provider: "",
      api_url: "",
      api_key_set: false,
      model: "",
    },
    api_tokens: [],
  },
});

describe("+page.svelte URL state & Read history initial render", () => {
  it("restores ?view=decisions&filter=read with a loading state instead of a false empty state", () => {
    mockPage.url = new URL("http://bookward.test/?view=decisions&filter=read");
    mockPage.shallow = null;

    const { body } = render(Page, {
      props: {
        data: createPageData() as never,
        form: null,
      },
    });

    expect(body).toContain("Your reading history.");
    expect(body).toContain("Loading your read books");
    expect(body).not.toContain("No read books yet");
  });

  it("restores ?view=saved directly into the Shortlist view", () => {
    mockPage.url = new URL("http://bookward.test/?view=saved");
    mockPage.shallow = null;

    const { body } = render(Page, {
      props: {
        data: createPageData() as never,
        form: null,
      },
    });

    expect(body).toContain("Your shortlist.");
    expect(body).toContain('aria-label="Search shortlist"');
  });

  it("distinguishes persisted recovery hold banner copy from unreadable hold status in Settings", () => {
    mockPage.url = new URL("http://bookward.test/?view=settings");
    mockPage.shallow = null;

    const persistedData = createPageData();
    Object.assign(persistedData.backupStatus, {
      enabled: true,
      recovery_hold: true,
      recovery_hold_at: "2026-11-21T20:00:00+00:00",
      backups: [
        {
          id: "bookward-20261120T190000Z-aaaa1111",
          created_at: "2026-11-20T19:00:00+00:00",
          size_bytes: 65536,
        },
      ],
    });
    const persistedRender = render(Page, {
      props: {
        data: persistedData as never,
        form: null,
      },
    });
    expect(persistedRender.body).toContain("Recovery hold · backups paused");
    expect(persistedRender.body).toContain("Bookward initialized a new profile database");
    expect(persistedRender.body).toContain("?/dismissRecoveryHold");

    const unreadableData = createPageData();
    Object.assign(unreadableData.backupStatus, {
      enabled: true,
      recovery_hold: true,
      recovery_hold_at: null,
      last_error: "Backup status is temporarily unavailable.",
      backups: [
        {
          id: "bookward-20261120T190000Z-aaaa1111",
          created_at: "2026-11-20T19:00:00+00:00",
          size_bytes: 65536,
        },
      ],
    });
    const unreadableRender = render(Page, {
      props: {
        data: unreadableData as never,
        form: null,
      },
    });
    expect(unreadableRender.body).toContain("Recovery hold · backups paused");
    expect(unreadableRender.body).toContain(
      "Scheduled backups paused — recovery hold status could not be read",
    );
    expect(unreadableRender.body).toContain(
      "Bookward could not read recovery-hold status from the current profile database",
    );
    expect(unreadableRender.body).not.toContain("initialized a new profile database");
    expect(unreadableRender.body).not.toContain("?/dismissRecoveryHold");
  });
});

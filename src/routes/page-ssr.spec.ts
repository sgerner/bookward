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
  decisions: [],
  decisions_has_more: false,
  history: [],
  readCount: 12,
  backupStatus: {
    configured: false,
    enabled: false,
    directory: "",
    interval_hours: 24,
    keep_count: 14,
    last_backup_at: null,
    last_backup_file: null,
    last_error: null,
    next_backup_at: null,
    backups: [],
  },
  digestReview: { requested: false, ids: [] },
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
      frequency: "weekly",
      weekday: 5,
      hour_utc: 14,
      limit: 5,
      only_new: true,
      channels: {
        discord_enabled: false,
        discord_webhook_set: false,
        email_enabled: false,
        smtp_host: "",
        smtp_port: 587,
        smtp_username: "",
        smtp_password_set: false,
        smtp_use_tls: true,
        email_from: "",
        email_to: "",
      },
      last_sent_at: null,
      last_period_key: null,
      last_error: null,
      next_Scheduled_at: null,
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
});

import { describe, expect, it } from "vitest";
import {
  canRunDigestNow,
  canTestDigestChannel,
  hasUnsavedDigestChanges,
  isBoundedDigestReview,
  pruneDigestSelection,
  resolveDigestBulkRunId,
  shouldPaginateDiscover,
  shouldShowDigestTestButton,
  type DigestFormDraft,
  type SavedDigestSettings,
} from "./digest-ui";

function baseSaved(): SavedDigestSettings {
  return {
    enabled: false,
    channels: [],
    day: 1,
    time: "09:00",
    timezone: "UTC",
    minimum_score: 80,
    maximum_books: 5,
    only_new: true,
    app_url: "https://bookward.example.org",
    discord_webhook_set: false,
    email_to: "",
    email_from: "",
    smtp_host: "",
    smtp_port: 587,
    smtp_security: "starttls",
    smtp_username_set: false,
    smtp_password_set: false,
  };
}

function draftFromSaved(saved: SavedDigestSettings): DigestFormDraft {
  return {
    enabled: saved.enabled,
    discord: saved.channels.includes("discord"),
    email: saved.channels.includes("email"),
    day: saved.day,
    time: saved.time,
    timezone: saved.timezone,
    minimumScore: String(saved.minimum_score),
    maximumBooks: String(saved.maximum_books),
    onlyNew: saved.only_new,
    appUrl: saved.app_url,
    discordWebhook: "",
    clearDiscord: false,
    emailTo: saved.email_to,
    emailFrom: saved.email_from,
    smtpHost: saved.smtp_host,
    smtpPort: String(saved.smtp_port),
    smtpSecurity: saved.smtp_security,
    smtpUsername: "",
    smtpPassword: "",
    clearSmtpCredentials: false,
  };
}

describe("digest settings dirty guard and action controls", () => {
  it("disables test and run buttons while channel toggles or inputs have unsaved edits", () => {
    const saved = baseSaved();
    const draft = draftFromSaved(saved);
    expect(hasUnsavedDigestChanges(saved, draft)).toBe(false);
    expect(shouldShowDigestTestButton(saved, draft, "discord")).toBe(false);
    expect(canRunDigestNow(saved, draft)).toBe(false);

    // Toggle Discord on and type webhook before saving while schedule remains off.
    draft.discord = true;
    draft.discordWebhook = "https://discord.com/api/webhooks/123/abc";
    expect(hasUnsavedDigestChanges(saved, draft)).toBe(true);
    expect(shouldShowDigestTestButton(saved, draft, "discord")).toBe(true);
    expect(canTestDigestChannel(saved, draft, "discord")).toBe(false);
    expect(canRunDigestNow(saved, draft)).toBe(false);
  });

  it("enables channel test and manual run once channel is saved even when schedule stays off", () => {
    const saved: SavedDigestSettings = {
      ...baseSaved(),
      enabled: false,
      channels: ["discord"],
      discord_webhook_set: true,
    };
    const draft = draftFromSaved(saved);
    expect(hasUnsavedDigestChanges(saved, draft)).toBe(false);
    expect(canTestDigestChannel(saved, draft, "discord")).toBe(true);
    expect(canRunDigestNow(saved, draft)).toBe(true);

    // Editing any input marks dirty and disables test/run again until saved.
    draft.minimumScore = "85";
    expect(hasUnsavedDigestChanges(saved, draft)).toBe(true);
    expect(canTestDigestChannel(saved, draft, "discord")).toBe(false);
    expect(canRunDigestNow(saved, draft)).toBe(false);
  });
});

describe("digest review selection pruning, run attribution, and bounded pagination", () => {
  it("prunes passed and shortlisted books from selectedDigestIds", () => {
    const books = [
      { id: 1, status: "recommended", recommendation_run_id: "run-abcd-1234" },
      { id: 2, status: "rejected", recommendation_run_id: "run-abcd-1234" },
      { id: 3, status: "recommended", recommendation_run_id: "run-abcd-1234" },
      { id: 4, status: "saved", recommendation_run_id: "run-abcd-1234" },
    ];
    const pruned = pruneDigestSelection([1, 2, 3, 4], books);
    expect([...pruned]).toEqual([1, 3]);
    expect(resolveDigestBulkRunId(pruned, books, "run-abcd-1234")).toBe("run-abcd-1234");
  });

  it("suppresses extra discovery pagination when viewing a bounded digest period", () => {
    const bounded = isBoundedDigestReview(true, true);
    expect(bounded).toBe(true);
    expect(
      shouldPaginateDiscover({
        isDiscoverView: true,
        boundedDigestReview: bounded,
        visibleCount: 15,
        totalFilteredCount: 15,
        discoverHasMore: true,
      }),
    ).toBe(false);

    expect(
      shouldPaginateDiscover({
        isDiscoverView: true,
        boundedDigestReview: false,
        visibleCount: 8,
        totalFilteredCount: 15,
        discoverHasMore: true,
      }),
    ).toBe(true);
  });
});

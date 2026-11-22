export type SavedDigestSettings = {
  enabled: boolean;
  channels: Array<"discord" | "email">;
  day: number;
  time: string;
  timezone: string;
  minimum_score: number;
  maximum_books: number;
  only_new: boolean;
  app_url: string;
  discord_webhook_set: boolean;
  email_to: string;
  email_from: string;
  smtp_host: string;
  smtp_port: number;
  smtp_security: "none" | "starttls" | "ssl";
  smtp_username_set: boolean;
  smtp_password_set: boolean;
};

export type DigestFormDraft = {
  enabled: boolean;
  discord: boolean;
  email: boolean;
  day: number;
  time: string;
  timezone: string;
  minimumScore: string;
  maximumBooks: string;
  onlyNew: boolean;
  appUrl: string;
  discordWebhook: string;
  clearDiscord: boolean;
  emailTo: string;
  emailFrom: string;
  smtpHost: string;
  smtpPort: string;
  smtpSecurity: "none" | "starttls" | "ssl";
  smtpUsername: string;
  smtpPassword: string;
  clearSmtpCredentials: boolean;
};

function normalizeUrl(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

export function hasUnsavedDigestChanges(
  saved: SavedDigestSettings,
  draft: DigestFormDraft,
): boolean {
  if (draft.enabled !== Boolean(saved.enabled)) return true;
  if (draft.onlyNew !== Boolean(saved.only_new)) return true;
  const savedChannels = Array.isArray(saved.channels) ? saved.channels : [];
  const savedDiscord = savedChannels.includes("discord");
  const savedEmail = savedChannels.includes("email");
  if (draft.discord !== savedDiscord || draft.email !== savedEmail) return true;
  if (Number(draft.day) !== Number(saved.day)) return true;
  if (draft.time.trim() !== String(saved.time ?? "").trim()) return true;
  if (draft.timezone.trim() !== String(saved.timezone ?? "").trim()) return true;
  const minScore = draft.minimumScore.trim();
  if (!minScore || Number(minScore) !== Number(saved.minimum_score)) return true;
  const maxBooks = draft.maximumBooks.trim();
  if (!maxBooks || Number(maxBooks) !== Number(saved.maximum_books)) return true;
  if (normalizeUrl(draft.appUrl) !== normalizeUrl(String(saved.app_url ?? ""))) return true;
  if (draft.discord && (draft.discordWebhook.trim() !== "" || draft.clearDiscord)) {
    return true;
  }
  if (draft.emailTo.trim() !== String(saved.email_to ?? "").trim()) return true;
  if (draft.emailFrom.trim() !== String(saved.email_from ?? "").trim()) return true;
  if (draft.smtpHost.trim() !== String(saved.smtp_host ?? "").trim()) return true;
  const port = draft.smtpPort.trim();
  if (!port || Number(port) !== Number(saved.smtp_port)) return true;
  if (draft.smtpSecurity !== saved.smtp_security) return true;
  if (
    draft.email &&
    (draft.smtpUsername.trim() !== "" ||
      draft.smtpPassword !== "" ||
      draft.clearSmtpCredentials)
  ) {
    return true;
  }
  return false;
}

export function shouldShowDigestTestButton(
  saved: SavedDigestSettings,
  draft: DigestFormDraft,
  channel: "discord" | "email",
): boolean {
  const savedChannels = Array.isArray(saved.channels) ? saved.channels : [];
  return channel === "discord"
    ? savedChannels.includes("discord") || draft.discord
    : savedChannels.includes("email") || draft.email;
}

export function canTestDigestChannel(
  saved: SavedDigestSettings,
  draft: DigestFormDraft,
  channel: "discord" | "email",
): boolean {
  const savedChannels = Array.isArray(saved.channels) ? saved.channels : [];
  const active =
    channel === "discord"
      ? savedChannels.includes("discord") && draft.discord
      : savedChannels.includes("email") && draft.email;
  return active && !hasUnsavedDigestChanges(saved, draft);
}

export function canRunDigestNow(
  saved: SavedDigestSettings,
  draft: DigestFormDraft,
): boolean {
  const savedChannels = Array.isArray(saved.channels) ? saved.channels : [];
  return (
    savedChannels.length > 0 &&
    (draft.discord || draft.email) &&
    !hasUnsavedDigestChanges(saved, draft)
  );
}

export function pruneDigestSelection(
  selectedIds: Iterable<number>,
  books: ReadonlyArray<{ id: number; status: string }>,
): Set<number> {
  const recommended = new Set(
    books.filter((book) => book.status === "recommended").map((book) => book.id),
  );
  const next = new Set<number>();
  for (const id of selectedIds) {
    if (recommended.has(id)) next.add(id);
  }
  return next;
}

export function resolveDigestBulkRunId(
  selectedIds: Iterable<number>,
  books: ReadonlyArray<{ id: number; recommendation_run_id?: string | null }>,
  fallbackRunId = "",
): string {
  const byId = new Map(
    books.map((book) => [book.id, String(book.recommendation_run_id || "").trim()]),
  );
  const ids = [...selectedIds];
  if (!ids.length) {
    return fallbackRunId.trim().length >= 8 ? fallbackRunId.trim() : "";
  }
  const runIds = new Set<string>();
  for (const id of ids) {
    const runId = byId.get(id) ?? "";
    if (runId.length < 8) return "";
    runIds.add(runId);
  }
  return runIds.size === 1 ? [...runIds][0] : "";
}

export function isBoundedDigestReview(
  digestVisible: boolean,
  digestReviewRequested: boolean,
): boolean {
  return Boolean(digestVisible && digestReviewRequested);
}

export function shouldPaginateDiscover(options: {
  isDiscoverView: boolean;
  boundedDigestReview: boolean;
  visibleCount: number;
  totalFilteredCount: number;
  discoverHasMore: boolean;
}): boolean {
  if (!options.isDiscoverView || options.boundedDigestReview) return false;
  return options.visibleCount < options.totalFilteredCount || options.discoverHasMore;
}

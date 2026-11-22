export type LlmCatalogProviderChoice = {
  id: string;
  name: string;
  api: string;
  npm?: string;
  models: Array<{ id: string; name: string }>;
};

export type LlmRerankSettingsView = {
  enabled: boolean;
  provider: string;
  api_url: string;
  model: string;
  api_key_set: boolean;
  last_status?: string;
  last_error?: string;
  last_attempt_at?: string;
  last_success_at?: string;
  cooldown_until?: string;
};

export type LlmRerankOverrides = {
  enabled: boolean | null;
  provider: string | null;
  apiUrl: string | null;
  model: string | null;
};

export function selectLlmCatalogProvider(
  providerId: string,
  catalogProviders: ReadonlyArray<LlmCatalogProviderChoice>,
): {
  provider: string;
  apiUrl: string | null;
  model: string | null;
} {
  if (!providerId) {
    return { provider: "", apiUrl: "", model: null };
  }
  const match = catalogProviders.find((item) => item.id === providerId);
  if (!match) {
    return { provider: providerId, apiUrl: null, model: null };
  }
  return {
    provider: providerId,
    apiUrl: match.api ? match.api : "",
    model: match.models.length > 0 ? match.models[0].id : "",
  };
}

export function llmCardStatus(settings: {
  enabled: boolean;
  last_status?: string;
  last_error?: string;
}) {
  if (!settings.enabled) {
    return {
      state: "disabled" as const,
      label: "Disabled",
      badgeClass: "preset-tonal-surface",
      degraded: false,
    };
  }
  if (settings.last_status === "failed" || Boolean(settings.last_error?.trim())) {
    return {
      state: "degraded" as const,
      label: "Degraded",
      badgeClass: "preset-tonal-warning",
      degraded: true,
    };
  }
  return {
    state: "enabled" as const,
    label: "Enabled",
    badgeClass: "preset-tonal-success",
    degraded: false,
  };
}

export function applyLlmRerankFormOverrides(formData: FormData): LlmRerankOverrides {
  return {
    enabled: formData.get("enabled") === "on",
    provider: String(formData.get("provider") ?? "").trim(),
    apiUrl: String(formData.get("apiUrl") ?? "").trim(),
    model: String(formData.get("model") ?? "").trim(),
  };
}

export function resetLlmRerankOverrides(): LlmRerankOverrides {
  return {
    enabled: null,
    provider: null,
    apiUrl: null,
    model: null,
  };
}

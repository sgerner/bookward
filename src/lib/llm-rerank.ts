export const LLM_RERANK_MODES = ["cloud", "ollama", "openai-compatible"] as const;

export type LlmRerankMode = (typeof LLM_RERANK_MODES)[number];

export const INVALID_LLM_RERANK_MODE_MESSAGE = "Choose a valid LLM reranker mode.";

const LLM_RERANK_MODE_SET: ReadonlySet<string> = new Set(LLM_RERANK_MODES);

export function parseLlmRerankMode(
  value: unknown,
  fallback?: LlmRerankMode,
): LlmRerankMode | null {
  if (value === undefined || value === null) {
    return fallback ?? null;
  }
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return fallback ?? null;
  }
  return LLM_RERANK_MODE_SET.has(normalized)
    ? (normalized as LlmRerankMode)
    : null;
}

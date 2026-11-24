export type SourceScanTone = "healthy" | "warning" | "error" | "idle";

export function formatScanDateLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.valueOf())) return null;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function sourceScanTone(
  value: string | null | undefined,
  status: string | null | undefined,
): SourceScanTone {
  if (status?.startsWith("error:")) return "error";
  if (status === "empty:0" || status === "filtered:0") return "warning";
  if (value) return "healthy";
  return "idle";
}

export function formatSourceScanStatus(
  value: string | null | undefined,
  status: string | null | undefined,
): string {
  const goodDate = formatScanDateLabel(value);
  if (status?.startsWith("error:")) {
    const detail = status.slice("error:".length).trim().replace(/\s+/g, " ");
    const clipped = detail.length > 140 ? `${detail.slice(0, 139)}…` : detail;
    const errorLabel = clipped ? `Needs attention: ${clipped}` : "Needs attention";
    return goodDate ? `Last good scan ${goodDate} · ${errorLabel}` : errorLabel;
  }
  if (status === "empty:0") {
    const warning = "Warning: upstream feed returned 0 books (existing candidates kept)";
    return goodDate ? `Last good scan ${goodDate} · ${warning}` : warning;
  }
  if (status === "filtered:0") {
    const warning = "Warning: 0 books matched genre filters (existing candidates kept)";
    return goodDate ? `Last good scan ${goodDate} · ${warning}` : warning;
  }
  if (!value) return "Not scanned yet";
  return goodDate ? `Scanned ${goodDate}` : "Scanned";
}

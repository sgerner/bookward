const READING_SOURCE_LABELS: Record<string, string> = {
  goodreads_csv: "Goodreads CSV export",
  storygraph_csv: "StoryGraph CSV export",
  goodreads_rss: "Goodreads read shelf RSS",
  manual: "Added in Bookward",
  demo: "Demo data",
};

export function formatReadingSourceLabel(source: string): string {
  return READING_SOURCE_LABELS[source] ?? (source || "Unknown source");
}

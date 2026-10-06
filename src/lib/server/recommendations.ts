export type Recommendation = {
  id: number;
  recommendation_run_id?: string;
  title: string;
  author: string;
  description: string;
  cover_url: string;
  source_url: string;
  release_date: string | null;
  date_kind: string;
  genres: string[];
  score: number;
  explanation: string[];
  status: string;
  source_name: string | null;
};

const publicUrl = (value: string) => {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
};

export function toPageBook(book: Recommendation) {
  return {
    ...book,
    recommendation_run_id: book.recommendation_run_id ?? "",
    cover_url: publicUrl(book.cover_url),
    source_url: publicUrl(book.source_url),
    published_on: book.release_date,
    published_kind: book.date_kind,
    synopsis: book.description,
    reason: book.explanation.join(" · "),
    source_type: "engine" as const,
  };
}

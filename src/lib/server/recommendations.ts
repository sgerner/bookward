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
  metadata_confidence?: number | null;
  explanation: string[];
  status: string;
  source_id?: number | null;
  source_name: string | null;
  source_enabled?: boolean;
  quality_status?: string | null;
};

export type PageBook = {
  id: number;
  recommendation_run_id: string;
  title: string;
  author: string;
  cover_url: string;
  source_url: string;
  source_id: number | null;
  source_name: string | null;
  source_enabled: boolean;
  quality_status: string | null;
  published_on: string | null;
  published_kind: string;
  genres: string[];
  score: number;
  metadata_confidence: number | null;
  synopsis: string;
  reason: string;
  status: string;
};

const publicUrl = (value: string) => {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
};

export function toPageBook(book: Recommendation): PageBook {
  return {
    id: book.id,
    recommendation_run_id: book.recommendation_run_id ?? "",
    title: book.title,
    author: book.author,
    cover_url: publicUrl(book.cover_url),
    source_url: publicUrl(book.source_url),
    source_id: book.source_id ?? null,
    source_name: book.source_name ?? null,
    source_enabled: book.source_enabled !== false,
    quality_status: book.quality_status ?? null,
    published_on: book.release_date,
    published_kind: book.date_kind,
    genres: book.genres,
    score: book.score,
    metadata_confidence: book.metadata_confidence ?? null,
    synopsis: book.description,
    reason: book.explanation.join(" · "),
    status: book.status,
  };
}

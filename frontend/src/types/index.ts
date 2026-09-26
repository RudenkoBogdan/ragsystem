export interface User {
  id: number;
  username: string;
}

export interface Paper {
  id: number;
  arxiv_id: string;
  title: string;
  authors: string;
  abstract: string | null;
  year: number | null;
  url: string | null;
  created_at: string;
}

export interface ChatSession {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface SourceRef {
  title: string;
  arxiv_id: string;
  page: number;
  // Verifiable citations. All four are OPTIONAL on purpose: message rows were
  // persisted before this feature existed and carry none of them, so every
  // read site must tolerate `undefined`/`null` and fall back to the legacy
  // numbering (array position + 1).
  /** The number the model was shown. Authoritative; never the array index. */
  label?: number | null;
  /** The actual retrieved text backing this citation. */
  snippet?: string | null;
  /** Cosine similarity, ~0..1. */
  score?: number | null;
  /** How many passages from this page were merged into this citation. */
  chunk_count?: number | null;
}

/** Which source numbers the finished answer really used. */
export interface CitationMeta {
  /** Labels that were cited and exist, in reading order. */
  cited: number[];
  /** Labels that were cited but are not in the retrieved set. */
  unresolved: number[];
}

/** Which of the question's key terms the retrieved pages actually contain. */
export interface CoverageInfo {
  /** Number of distinctive terms in the question. Never capped. */
  total: number;
  /** How many of them appear in the retrieved passages. Never capped. */
  covered: number;
  /** Covered term names, question order, capped at 12. */
  terms: string[];
  /** Terms absent from the retrieved passages, question order, capped at 12. */
  missing: string[];
}

/** What retrieval was actually restricted to. */
export interface ScopeInfo {
  applied: boolean;
  paper_ids: number[];
  paper_titles: string[];
}

/**
 * The honest part of an answer: what was searched, whether anything came back,
 * and how much of what you asked for the evidence actually mentions.
 *
 * This is a *lexical* measurement, not a quality or confidence score. It reads
 * low on a correct answer whose vocabulary differs from the paper's, and high on
 * a wrong answer that echoes your question. It is shown as a fact about the
 * retrieved pages, never as a verdict about the answer.
 */
export interface RetrievalMeta {
  scope: ScopeInfo;
  /** null when there were too few key terms to say anything meaningful. */
  coverage: CoverageInfo | null;
  /**
   * The coverage line rendered by the BACKEND, already worded and unit-tested
   * in Python. It is not re-derived here on purpose: a second implementation
   * would be free to drift, and then the tests would be guarding a string no
   * user ever sees. Only used as a fallback if it is absent.
   */
  coverageLine: string | null;
  /** true when nothing was retrieved and no LLM call was made at all. */
  abstained: boolean;
}

/** Options for `sendMessageStream`. An object, not more positional arguments. */
export interface SendOptions {
  apiKey?: string;
  model?: string;
  provider?: string;
  baseUrl?: string;
  /** Restrict retrieval to these papers. Omitted when empty. */
  paperIds?: number[];
}

/** The POST body for a chat message. */
export interface MessageBody {
  content: string;
  api_key?: string;
  model?: string;
  provider?: string;
  base_url?: string;
  paper_ids?: number[];
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: SourceRef[];
  created_at: string;
  /**
   * Live-only citation verification, delivered on the `done` SSE event.
   * The backend does NOT persist it (no column, and `create_all` never alters
   * an existing table), so it is absent on every message loaded from history:
   * after a reload the bubble simply shows no citation markers. That is an
   * accepted limitation, not a bug.
   */
  citationMeta?: CitationMeta;
  /**
   * Live-only retrieval transparency, delivered on the same `done` event and
   * for the same reason: `Message.sources` is the only column that could hold
   * it, and adding one is not possible on an existing database. So the scope
   * and coverage readout disappear on reload -- but the sources themselves do
   * not, so the papers that answered the question remain readable afterwards.
   */
  retrieval?: RetrievalMeta;
}

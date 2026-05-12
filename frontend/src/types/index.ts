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
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: SourceRef[];
  created_at: string;
}

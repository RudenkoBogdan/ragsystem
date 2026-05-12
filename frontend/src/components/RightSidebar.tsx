"use client";
import { useState, useRef } from "react";
import { Plus, Trash2, BookOpen, Loader2, ExternalLink } from "lucide-react";
import { papersApi } from "@/lib/api";
import type { Paper } from "@/types";

interface Props {
  papers: Paper[];
  onAddPaper: (paper: Paper) => void;
  onDeletePaper: (id: number) => void;
}

export default function RightSidebar({ papers, onAddPaper, onDeletePaper }: Props) {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleAdd(e?: React.FormEvent) {
    e?.preventDefault();
    if (!url.trim()) return;
    setError("");
    setLoading(true);
    try {
      const paper = await papersApi.add(url.trim());
      onAddPaper(paper);
      setUrl("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to add paper");
    } finally {
      setLoading(false);
    }
  }

  async function handlePaste() {
    try {
      const text = await navigator.clipboard.readText();
      setUrl(text);
      inputRef.current?.focus();
    } catch {
      inputRef.current?.focus();
    }
  }

  return (
    <div className="flex w-72 flex-col border-l border-border bg-bg-secondary">
      {/* Header */}
      <div className="border-b border-border px-4 py-4">
        <div className="flex items-center gap-2">
          <BookOpen className="h-4 w-4 text-accent-blue" />
          <h2 className="text-sm font-semibold text-text-primary">Papers</h2>
          <span className="ml-auto rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary">
            {papers.length}
          </span>
        </div>
      </div>

      {/* Add paper form */}
      <div className="border-b border-border px-3 py-3">
        <form onSubmit={handleAdd} className="space-y-2">
          <div className="flex gap-1.5">
            <input
              ref={inputRef}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="arXiv URL or ID..."
              className="flex-1 min-w-0 rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder-text-muted outline-none focus:border-accent-blue transition-colors"
            />
            <button
              type="button"
              onClick={handlePaste}
              title="Paste from clipboard"
              className="rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary hover:border-accent-blue transition-colors"
            >
              Paste
            </button>
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={loading || !url.trim()}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent-blue py-1.5 text-xs font-medium text-white hover:bg-accent-blue-hover disabled:opacity-50 transition-colors"
          >
            {loading ? (
              <>
                <Loader2 className="h-3 w-3 animate-spin" />
                Ingesting...
              </>
            ) : (
              <>
                <Plus className="h-3 w-3" />
                Add Paper
              </>
            )}
          </button>
        </form>
      </div>

      {/* Papers list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {papers.length === 0 && (
          <div className="py-8 text-center">
            <BookOpen className="mx-auto h-8 w-8 text-text-muted opacity-40" />
            <p className="mt-2 text-xs text-text-muted">No papers yet</p>
            <p className="mt-1 text-xs text-text-muted">Paste an arXiv link to get started</p>
          </div>
        )}
        {papers.map((paper) => (
          <PaperCard key={paper.id} paper={paper} onDelete={onDeletePaper} />
        ))}
      </div>
    </div>
  );
}

function PaperCard({ paper, onDelete }: { paper: Paper; onDelete: (id: number) => void }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-lg border border-border bg-bg-tertiary p-3 group">
      <div className="flex items-start justify-between gap-2 min-w-0">
        <button
          className="flex-1 text-left min-w-0"
          onClick={() => setExpanded((v) => !v)}
        >
          <p className="text-xs font-medium text-text-primary leading-snug line-clamp-2 break-words">
            {paper.title}
          </p>
          <p className="mt-1 text-xs text-text-secondary truncate">{paper.authors}</p>
          <div className="mt-1 flex items-center gap-1 flex-wrap">
            {paper.year && (
              <span className="text-xs text-text-muted">{paper.year}</span>
            )}
            <span className="text-xs text-text-muted font-mono break-all">{paper.arxiv_id}</span>
          </div>
        </button>
        <div className="flex gap-1 flex-shrink-0">
          {paper.url && (
            <a
              href={paper.url}
              target="_blank"
              rel="noopener noreferrer"
              title="Open on arXiv"
              className="h-6 w-6 flex items-center justify-center rounded text-text-muted hover:text-accent-blue transition-colors"
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
          <button
            onClick={() => onDelete(paper.id)}
            title="Delete paper"
            className="h-6 w-6 flex items-center justify-center rounded text-text-muted hover:text-red-400 transition-colors"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>
      {expanded && paper.abstract && (
        <p className="mt-2 text-xs text-text-secondary leading-relaxed border-t border-border pt-2 break-words">
          {paper.abstract.slice(0, 300)}
          {paper.abstract.length > 300 ? "..." : ""}
        </p>
      )}
    </div>
  );
}

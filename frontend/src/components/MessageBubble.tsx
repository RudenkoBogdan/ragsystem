"use client";
import { useState } from "react";
// Only icons already used elsewhere in this codebase are imported: the pinned
// `lucide-react` cannot be checked from here, and an unresolved icon import is
// a build failure with no local signal.
import { Check, ChevronDown, ExternalLink, FileText } from "lucide-react";
import type { CoverageInfo, Message, SourceRef } from "@/types";
import clsx from "clsx";
import MathContent from "./MathContent";

interface Props {
  message: Message;
}

/** The label the model was shown for this source, with a legacy fallback. */
function sourceLabel(src: SourceRef, index: number): number {
  // Messages persisted before verifiable citations carry no `label`; their
  // numbering was the deduplicated array position, which is exactly `i + 1`.
  return typeof src.label === "number" ? src.label : index + 1;
}

/** Cosine similarity -> a clamped 0..100 percentage for the relevance bar. */
function scorePercent(score: number): number {
  const clamped = Math.max(0, Math.min(1, score));
  return Math.round(clamped * 100);
}

/**
 * Fallback renderer, used only when the backend sent no `coverage_line` -- i.e.
 * an older deployment. The wording that ships is produced and tested in
 * `backend/chat/coverage.py`; this exists so a mixed-version pair degrades to
 * something readable rather than to nothing.
 */
function coverageLine(coverage: CoverageInfo): string {
  const found = coverage.terms.length > 0 ? `: ${coverage.terms.join(", ")}` : "";
  const more =
    coverage.covered > coverage.terms.length
      ? ` and ${coverage.covered - coverage.terms.length} more`
      : "";
  const missing =
    coverage.missing.length > 0
      ? ` - not found in the retrieved pages: ${coverage.missing.join(", ")}`
      : "";
  return `covered ${coverage.covered} of ${coverage.total} key terms${found}${more}${missing}`;
}

const COVERAGE_EXPLANATION =
  "Which of the key terms in your question actually appear in the pages this answer " +
  "was built from. It is a lexical measurement of the retrieved text, not a " +
  "confidence score: an answer can be correct with low coverage (the paper words " +
  "things differently), and a wrong answer can score high (it echoes your question).";

export default function MessageBubble({ message }: Props) {
  const isUser = message.role === "user";
  // Index of the source whose evidence panel is open, or null. Only one at a
  // time, so the panel never leaves the list visually ambiguous.
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  // Absent for every message loaded from history (never persisted), so the
  // cited/unresolved markers simply do not appear after a reload.
  const citationMeta = message.citationMeta;
  const cited = new Set<number>(citationMeta?.cited ?? []);
  const unresolved = citationMeta?.unresolved ?? [];
  // Live-only for the same reason, so a reloaded message shows neither the
  // scope nor the coverage line. The sources themselves DO survive a reload,
  // so the papers that answered the question remain readable afterwards.
  const retrieval = message.retrieval;

  return (
    <div className={clsx("flex", isUser ? "justify-end" : "justify-start")}>
      <div className={clsx("max-w-[80%] space-y-2", isUser ? "items-end" : "items-start")}>
        {/* Role label */}
        <p className={clsx("text-xs", isUser ? "text-right text-text-muted" : "text-text-muted")}>
          {isUser ? "You" : "Assistant"}
        </p>

        {/* Bubble */}
        <div
          className={clsx(
            "rounded-2xl px-4 py-3 text-sm leading-relaxed",
            isUser
              ? "bg-accent-blue text-white rounded-tr-sm"
              : "bg-bg-secondary border border-border text-text-primary rounded-tl-sm"
          )}
        >
          {message.content ? (
            isUser ? (
              <p className="whitespace-pre-wrap">{message.content}</p>
            ) : (
              <MathContent content={message.content} />
            )
          ) : (
            <span className="inline-block h-4 w-1 animate-pulse bg-current rounded" />
          )}
        </div>

        {/* Retrieval transparency: what was searched, and what came back. */}
        {retrieval && !isUser && (
          <div className="space-y-1 text-xs text-text-muted">
            {retrieval.scope.applied && retrieval.scope.paper_titles.length > 0 && (
              <p>
                Searched only {retrieval.scope.paper_titles.join(", ")}.
              </p>
            )}
            {retrieval.abstained && (
              <p>
                Nothing was retrieved, so no answer was generated from the model.
              </p>
            )}
            {retrieval.coverage && (
              <p className="flex items-start gap-1.5" title={COVERAGE_EXPLANATION}>
                <Check className="mt-0.5 h-3 w-3 flex-shrink-0" />
                <span>{retrieval.coverageLine ?? coverageLine(retrieval.coverage)}</span>
              </p>
            )}
          </div>
        )}

        {/* Sources */}
        {(message.sources.length > 0 || unresolved.length > 0) && (
          <div className="space-y-1">
            {message.sources.length > 0 && (
              <p className="text-xs text-text-muted">Sources:</p>
            )}

            {/* The model cited a number that was never retrieved. A trust
                signal, not an error: shown quietly, never blocking. */}
            {unresolved.length > 0 && (
              <p className="text-xs text-text-muted">
                The answer referenced {unresolved.map((n) => `[${n}]`).join(", ")} — not in the
                retrieved set.
              </p>
            )}

            {message.sources.map((src, i) => {
              const label = sourceLabel(src, i);
              const snippet = src.snippet ?? "";
              // EVERY source gets the same chip, the same panel and the same
              // "open the PDF at page N" action. A source persisted before
              // verifiable citations carries no `snippet`, so only the
              // evidence rows (snippet / score / chunk count) are omitted.
              // Degrading the row to a non-interactive line would silently
              // remove the PDF affordance for those older messages, which is a
              // regression in behaviour this feature must not introduce.
              const isOpen = openIndex === i;
              const isCited = cited.has(label);
              const score = typeof src.score === "number" ? scorePercent(src.score) : null;
              const merged = (src.chunk_count ?? 0) > 1 ? src.chunk_count : null;
              const pdfUrl = `https://arxiv.org/pdf/${src.arxiv_id}.pdf#page=${src.page}`;

              return (
                <div key={i} className="space-y-1">
                  <button
                    type="button"
                    onClick={() => setOpenIndex(isOpen ? null : i)}
                    aria-expanded={isOpen}
                    aria-controls={`evidence-${message.id}-${i}`}
                    aria-label={`Source ${label}: ${src.title}, page ${src.page}${
                      isCited ? ", cited in this answer" : ""
                    }`}
                    className={clsx(
                      "group flex w-full items-center gap-1.5 rounded-lg px-1.5 py-1 text-left text-xs transition-colors",
                      isOpen
                        ? "bg-bg-tertiary text-text-primary"
                        : "text-text-secondary hover:bg-bg-tertiary hover:text-text-primary"
                    )}
                  >
                    <span
                      className={clsx(
                        "flex-shrink-0 rounded px-1.5 py-0.5 font-mono transition-colors",
                        isOpen
                          ? "bg-accent-blue text-white"
                          : "bg-bg-tertiary text-accent-blue group-hover:bg-accent-blue group-hover:text-white"
                      )}
                    >
                      [{label}]
                    </span>
                    <span className="truncate flex-1 group-hover:underline">{src.title}</span>
                    {isCited && (
                      <span title="Cited in this answer" className="flex-shrink-0">
                        <Check className="h-3 w-3 text-accent-blue" />
                      </span>
                    )}
                    <span className="flex-shrink-0 font-mono text-text-muted">p.{src.page}</span>
                    <ChevronDown
                      className={clsx(
                        "flex-shrink-0 text-text-muted transition-transform",
                        isOpen && "rotate-180"
                      )}
                    />
                  </button>

                  {isOpen && (
                    <div
                      id={`evidence-${message.id}-${i}`}
                      className="space-y-2 rounded-lg border border-border bg-bg-tertiary p-3"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-xs font-medium text-text-primary leading-snug break-words">
                          {src.title}
                        </p>
                        <span className="flex-shrink-0 rounded bg-bg-secondary px-1.5 py-0.5 font-mono text-text-secondary">
                          p.{src.page}
                        </span>
                      </div>

                      {(score !== null || merged !== null) && (
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
                          {score !== null && (
                            <span className="flex items-center gap-1.5">
                              <span className="h-1 w-16 overflow-hidden rounded-full bg-bg-secondary">
                                <span
                                  className="block h-full rounded-full bg-accent-blue"
                                  style={{ width: `${score}%` }}
                                />
                              </span>
                              <span className="font-mono">{score}% match</span>
                            </span>
                          )}
                          {merged !== null && (
                            <span className="flex items-center gap-1.5">
                              <FileText className="h-3 w-3" />
                              {merged} passages from this page
                            </span>
                          )}
                        </div>
                      )}

                      {snippet && (
                        <p className="max-h-40 overflow-y-auto whitespace-pre-wrap border-t border-border pt-2 text-xs leading-relaxed text-text-secondary break-words">
                          {snippet}
                        </p>
                      )}

                      <a
                        href={pdfUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-accent-blue hover:text-accent-blue-hover transition-colors"
                      >
                        <ExternalLink className="h-3 w-3" />
                        Open PDF at page {src.page}
                      </a>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

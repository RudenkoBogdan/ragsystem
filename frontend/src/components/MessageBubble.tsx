"use client";
import type { Message } from "@/types";
import clsx from "clsx";
import MathContent from "./MathContent";

interface Props {
  message: Message;
  onSourceClick?: (source: Message["sources"][0], index: number) => void;
}

export default function MessageBubble({ message, onSourceClick }: Props) {
  const isUser = message.role === "user";

  const handleSourceClick = (source: Message["sources"][0], index: number) => {
    // Try to open PDF - this will be handled by parent component
    onSourceClick?.(source, index);
  };

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

        {/* Sources */}
        {message.sources.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-text-muted">Sources:</p>
            {message.sources.map((src, i) => (
              <div
                key={i}
                className="flex items-center gap-1.5 text-xs text-text-secondary cursor-pointer hover:text-accent-blue transition-colors group"
                onClick={() => handleSourceClick(src, i)}
              >
                <span className="flex-shrink-0 rounded bg-bg-tertiary px-1.5 py-0.5 font-mono text-accent-blue group-hover:bg-accent-blue group-hover:text-white transition-colors">
                  [{i + 1}]
                </span>
                <span className="truncate group-hover:underline flex-1">{src.title}</span>
                <span className="flex-shrink-0 text-text-muted">p.{src.page}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

"use client";
import { BlockMath } from "react-katex";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

interface Props {
  content: string;
}

const MD_COMPONENTS = {
  p: ({ children }: any) => <p className="mb-2 last:mb-0">{children}</p>,
  code: ({ inline, children }: any) =>
    inline ? (
      <code className="bg-bg-tertiary px-1.5 py-0.5 rounded text-xs font-mono">{children}</code>
    ) : (
      <pre className="block bg-bg-tertiary p-2 rounded text-xs font-mono overflow-x-auto mb-2">
        <code>{children}</code>
      </pre>
    ),
  strong: ({ children }: any) => <strong className="font-semibold">{children}</strong>,
  li: ({ children }: any) => <li className="ml-4 list-disc">{children}</li>,
};

function normalizeLatex(content: string): string {
  return content
    .replace(/\\\[/g, "$$").replace(/\\\]/g, "$$")
    .replace(/\\\(/g, "$").replace(/\\\)/g, "$");
}

export function hasMath(content: string): boolean {
  return /\$[\s\S]*?\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)/.test(content);
}

export default function MathContent({ content }: Props) {
  const normalized = normalizeLatex(content);
  const segments = normalized.split(/(\$\$[\s\S]*?\$\$)/);

  return (
    <div className="space-y-1">
      {segments.map((segment, i) => {
        if (segment.startsWith("$$") && segment.endsWith("$$") && segment.length > 4) {
          const math = segment.slice(2, -2).trim();
          return (
            <div key={i} className="my-3 text-center overflow-x-auto">
              <BlockMath math={math} />
            </div>
          );
        }
        if (!segment.trim()) return null;
        return (
          <ReactMarkdown
            key={i}
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[rehypeKatex]}
            components={MD_COMPONENTS}
          >
            {segment}
          </ReactMarkdown>
        );
      })}
    </div>
  );
}

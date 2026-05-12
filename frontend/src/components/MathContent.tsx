"use client";
import { InlineMath, BlockMath } from "react-katex";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "katex/dist/katex.min.css";

interface Props {
  content: string;
}

const MATH_PATTERN =
  /(\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$[^\$\n]+?\$)/g;

function isBlock(match: string): boolean {
  return match.startsWith("$$") || match.startsWith("\\[");
}

function extractMath(match: string): string {
  if (match.startsWith("$$")) return match.slice(2, -2).trim();
  if (match.startsWith("\\[")) return match.slice(2, -2).trim();
  if (match.startsWith("\\(")) return match.slice(2, -2).trim();
  return match.slice(1, -1).trim();
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

function renderInlineWithMath(text: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;
  let match: RegExpExecArray | null;

  MATH_PATTERN.lastIndex = 0;
  while ((match = MATH_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }

    const math = extractMath(match[0]);
    if (!isBlock(match[0])) {
      parts.push(<InlineMath key={`m${key++}`} math={math} />);
    }

    lastIndex = MATH_PATTERN.lastIndex;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts.length > 0 ? parts : text;
}

function renderWithMath(content: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  const paragraphs = content.split(/\n\n+/);
  let key = 0;

  paragraphs.forEach((para, idx) => {
    const lines = para.split("\n");
    const processedLines: React.ReactNode[] = [];

    lines.forEach((line, lineIdx) => {
      const blockMatches = Array.from(line.matchAll(/\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]/g));

      if (blockMatches.length > 0) {
        let lastIndex = 0;
        blockMatches.forEach((match) => {
          if (match.index! > lastIndex) {
            processedLines.push(
              <ReactMarkdown key={`t${key++}`} remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                {line.slice(lastIndex, match.index)}
              </ReactMarkdown>
            );
          }

          const math = extractMath(match[0]);
          processedLines.push(
            <div key={`m${key++}`} className="my-3 overflow-x-auto text-center">
              <BlockMath math={math} />
            </div>
          );
          lastIndex = match.index! + match[0].length;
        });

        if (lastIndex < line.length) {
          processedLines.push(
            <ReactMarkdown key={`t${key++}`} remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
              {line.slice(lastIndex)}
            </ReactMarkdown>
          );
        }
      } else {
        processedLines.push(
          <span key={`t${key++}`} className="inline">
            {renderInlineWithMath(line)}
          </span>
        );
      }
    });

    parts.push(
      <p key={`p${idx}`} className="mb-2 last:mb-0">
        {processedLines}
      </p>
    );
  });

  return <>{parts}</>;
}

export function hasMath(content: string): boolean {
  MATH_PATTERN.lastIndex = 0;
  return MATH_PATTERN.test(content);
}

export default function MathContent({ content }: Props) {
  return <div className="space-y-1">{renderWithMath(content)}</div>;
}

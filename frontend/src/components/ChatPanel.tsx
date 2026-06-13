"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import { Send, MessageSquare, Download } from "lucide-react";
import { sessionsApi, sendMessageStream } from "@/lib/api";
import type { ChatSession, Message } from "@/types";
import MessageBubble from "./MessageBubble";

interface Props {
  session: ChatSession | null;
  onSessionCreate: () => void;
  onSessionTitleUpdate: (id: number, title: string) => void;
}

export default function ChatPanel({ session, onSessionCreate, onSessionTitleUpdate }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const loadMessages = useCallback(async (sessionId: number) => {
    const msgs = await sessionsApi.getMessages(sessionId);
    setMessages(msgs);
  }, []);

  useEffect(() => {
    if (!session) {
      setMessages([]);
      return;
    }
    loadMessages(session.id);
  }, [session, loadMessages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const openSourcePDF = (source: Message["sources"][0]) => {
    // Open arxiv PDF at specific page
    const pdfUrl = `https://arxiv.org/pdf/${source.arxiv_id}.pdf#page=${source.page}`;
    window.open(pdfUrl, "_blank");
  };

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function send() {
    if (!input.trim() || streaming || !session) return;

    const userContent = input.trim();
    setInput("");
    setStreaming(true);

    const userMsg: Message = {
      id: Date.now(),
      role: "user",
      content: userContent,
      sources: [],
      created_at: new Date().toISOString(),
    };

    const assistantMsg: Message = {
      id: Date.now() + 1,
      role: "assistant",
      content: "",
      sources: [],
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);

    // Auto-update sidebar title from first message
    if (messages.length === 0) {
      onSessionTitleUpdate(session.id, userContent.slice(0, 60));
    }

    // Get custom settings from localStorage
    const ls = typeof window !== "undefined" ? localStorage : null;
    const provider = ls?.getItem("llm_provider") || "openrouter";

    let customApiKey: string | null = null;
    let customModel: string | null = null;
    let customBaseUrl: string | null = null;

    if (provider === "ollama") {
      customModel = ls?.getItem("ollama_model") || null;
      customBaseUrl = ls?.getItem("ollama_base_url") || null;
      customApiKey = ls?.getItem("ollama_api_key") || null;
    } else {
      customApiKey = ls?.getItem("openrouter_api_key") || null;
      customModel = ls?.getItem("openrouter_model") || null;
    }

    sendMessageStream(
      session.id,
      userContent,
      (token) => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsg.id ? { ...m, content: m.content + token } : m
          )
        );
      },
      (sources) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMsg.id ? { ...m, sources } : m))
        );
        setStreaming(false);
      },
      (err) => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsg.id ? { ...m, content: `Error: ${err}` } : m
          )
        );
        setStreaming(false);
      },
      customApiKey || undefined,
      customModel || undefined,
      provider,
      customBaseUrl || undefined
    );
  }

  if (!session) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-4 bg-bg-primary">
        <MessageSquare className="h-12 w-12 text-text-muted opacity-30" />
        <p className="text-text-secondary text-sm">Select a chat or create a new one</p>
        <button
          onClick={onSessionCreate}
          className="rounded-lg bg-accent-blue px-4 py-2 text-sm text-white hover:bg-accent-blue-hover transition-colors"
        >
          New Chat
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col bg-bg-primary overflow-hidden">
      {/* Header */}
      <div className="border-b border-border px-6 py-3 bg-bg-secondary">
        <h2 className="text-sm font-medium text-text-primary truncate">{session.title}</h2>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-6 space-y-6">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-center">
            <p className="text-text-secondary text-sm">Ask anything about your papers</p>
            <p className="text-text-muted text-xs">
              Add papers in the right sidebar, then start a conversation
            </p>
          </div>
        )}
        {messages.map((msg) => (
          <MessageBubble
            key={msg.id}
            message={msg}
            onSourceClick={(source) => openSourcePDF(source)}
          />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t border-border bg-bg-secondary px-4 py-4">
        <div className="flex items-center gap-3 rounded-xl border border-border bg-bg-tertiary px-4 py-3 focus-within:border-accent-blue transition-colors">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about your papers... (Enter to send, Shift+Enter for newline)"
            rows={1}
            disabled={streaming}
            className="flex-1 resize-none bg-transparent text-sm text-text-primary placeholder-text-muted outline-none disabled:opacity-50"
            style={{ maxHeight: "120px", overflowY: "auto", minHeight: "20px" }}
          />
          <button
            onClick={send}
            disabled={!input.trim() || streaming}
            className="flex-shrink-0 rounded-lg bg-accent-blue p-2 text-white hover:bg-accent-blue-hover disabled:opacity-40 transition-colors"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}

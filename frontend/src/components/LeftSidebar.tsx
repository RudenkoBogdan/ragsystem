"use client";
import { useRouter } from "next/navigation";
import { MessageSquare, Plus, Trash2, LogOut, FlaskConical, Settings } from "lucide-react";
import { clearToken } from "@/lib/auth";
import type { ChatSession } from "@/types";
import clsx from "clsx";

interface Props {
  sessions: ChatSession[];
  activeSession: ChatSession | null;
  onSelectSession: (s: ChatSession) => void;
  onNewChat: () => void;
  onDeleteSession: (id: number) => void;
}

export default function LeftSidebar({
  sessions,
  activeSession,
  onSelectSession,
  onNewChat,
  onDeleteSession,
}: Props) {
  const router = useRouter();

  function logout() {
    clearToken();
    router.push("/login");
  }

  return (
    <div className="flex w-64 flex-col border-r border-border bg-bg-secondary">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-4">
        <FlaskConical className="h-5 w-5 text-accent-blue" />
        <span className="font-semibold text-text-primary text-sm">Research Assistant</span>
      </div>

      {/* New Chat */}
      <div className="px-3 pt-3">
        <button
          onClick={onNewChat}
          className="flex w-full items-center gap-2 rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-secondary hover:border-accent-blue hover:text-text-primary transition-colors"
        >
          <Plus className="h-4 w-4" />
          New Chat
        </button>
      </div>

      {/* Sessions list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-0.5">
        {sessions.length === 0 && (
          <p className="px-2 py-4 text-xs text-text-muted text-center">No chats yet</p>
        )}
        {sessions.map((session) => (
          <div
            key={session.id}
            className={clsx(
              "group flex items-center gap-2 rounded-lg px-2 py-2 cursor-pointer transition-colors",
              activeSession?.id === session.id
                ? "bg-bg-tertiary text-text-primary"
                : "text-text-secondary hover:bg-bg-tertiary hover:text-text-primary"
            )}
            onClick={() => onSelectSession(session)}
          >
            <MessageSquare className="h-3.5 w-3.5 flex-shrink-0 opacity-60" />
            <span className="flex-1 truncate text-xs">{session.title}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDeleteSession(session.id);
              }}
              className="hidden group-hover:flex h-5 w-5 items-center justify-center rounded text-text-muted hover:text-red-400 transition-colors"
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        ))}
      </div>

      {/* Footer */}
      <div className="border-t border-border p-3 space-y-1">
        <button
          onClick={() => router.push("/settings")}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-text-secondary hover:bg-bg-tertiary hover:text-text-primary transition-colors"
        >
          <Settings className="h-4 w-4" />
          Settings
        </button>
        <button
          onClick={logout}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-text-secondary hover:bg-bg-tertiary hover:text-text-primary transition-colors"
        >
          <LogOut className="h-4 w-4" />
          Sign Out
        </button>
      </div>
    </div>
  );
}

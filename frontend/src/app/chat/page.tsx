"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { isAuthenticated } from "@/lib/auth";
import LeftSidebar from "@/components/LeftSidebar";
import ChatPanel from "@/components/ChatPanel";
import RightSidebar from "@/components/RightSidebar";
import type { ChatSession, Paper } from "@/types";
import { sessionsApi, papersApi } from "@/lib/api";

export default function ChatPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSession, setActiveSession] = useState<ChatSession | null>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isAuthenticated()) {
      router.replace("/login");
      return;
    }
    Promise.all([sessionsApi.list(), papersApi.list()]).then(([s, p]) => {
      setSessions(s);
      setPapers(p);
      if (s.length > 0) setActiveSession(s[0]);
      setLoading(false);
    });
  }, [router]);

  async function handleNewChat() {
    const session = await sessionsApi.create();
    setSessions((prev) => [session, ...prev]);
    setActiveSession(session);
  }

  async function handleDeleteSession(id: number) {
    await sessionsApi.remove(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
    if (activeSession?.id === id) {
      setActiveSession(sessions.find((s) => s.id !== id) ?? null);
    }
  }

  function handleSessionTitleUpdate(id: number, title: string) {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)));
  }

  async function handleAddPaper(paper: Paper) {
    setPapers((prev) => [paper, ...prev]);
  }

  async function handleDeletePaper(id: number) {
    await papersApi.remove(id);
    setPapers((prev) => prev.filter((p) => p.id !== id));
  }

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg-primary">
        <div className="text-text-secondary text-sm">Loading...</div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-bg-primary">
      <LeftSidebar
        sessions={sessions}
        activeSession={activeSession}
        onSelectSession={setActiveSession}
        onNewChat={handleNewChat}
        onDeleteSession={handleDeleteSession}
      />
      <ChatPanel
        session={activeSession}
        onSessionCreate={handleNewChat}
        onSessionTitleUpdate={handleSessionTitleUpdate}
      />
      <RightSidebar papers={papers} onAddPaper={handleAddPaper} onDeletePaper={handleDeletePaper} />
    </div>
  );
}

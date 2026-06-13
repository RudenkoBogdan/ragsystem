import { getToken } from "./auth";
import type { Paper, ChatSession, Message } from "@/types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Request failed");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// Auth
export const authApi = {
  register: (username: string, password: string) =>
    request<{ access_token: string }>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  login: (username: string, password: string) =>
    request<{ access_token: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<{ id: number; username: string }>("/api/auth/me"),
};

// Papers
export const papersApi = {
  list: () => request<Paper[]>("/api/papers"),
  add: (url: string) =>
    request<Paper>("/api/papers", { method: "POST", body: JSON.stringify({ url }) }),
  remove: (id: number) => request<void>(`/api/papers/${id}`, { method: "DELETE" }),
};

// Chat sessions
export const sessionsApi = {
  list: () => request<ChatSession[]>("/api/chat/sessions"),
  create: (title?: string) =>
    request<ChatSession>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ title: title ?? "New Chat" }),
    }),
  remove: (id: number) => request<void>(`/api/chat/sessions/${id}`, { method: "DELETE" }),
  getMessages: (sessionId: number) =>
    request<Message[]>(`/api/chat/sessions/${sessionId}/messages`),
};

// Streaming message send
export function sendMessageStream(
  sessionId: number,
  content: string,
  onToken: (token: string) => void,
  onDone: (sources: Message["sources"]) => void,
  onError: (err: string) => void,
  apiKey?: string,
  model?: string,
  provider?: string,
  baseUrl?: string
): void {
  const token = getToken();
  const body: any = { content };
  if (apiKey) body.api_key = apiKey;
  if (model) body.model = model;
  if (provider) body.provider = provider;
  if (baseUrl) body.base_url = baseUrl;

  fetch(`${BASE}/api/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  })
    .then(async (res) => {
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        onError(err.detail ?? "Request failed");
        return;
      }
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const event = JSON.parse(line.slice(6));
            if (event.type === "token") onToken(event.content);
            else if (event.type === "done") onDone(event.sources ?? []);
          } catch {
            // ignore malformed SSE lines
          }
        }
      }
    })
    .catch((err) => onError(err.message));
}

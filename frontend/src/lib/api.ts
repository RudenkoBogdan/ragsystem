import { getToken } from "./auth";
import type {
  Paper,
  ChatSession,
  CitationMeta,
  Message,
  MessageBody,
  RetrievalMeta,
  SendOptions,
} from "@/types";

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
  // `order=desc` selects the *newest* `limit` rows server-side and the handler
  // re-sorts them oldest-first before serialising, so this is the last page,
  // not a reversed conversation. Without it the backend's default page is the
  // first 200 messages, which silently drops the most recent turns of a long
  // chat -- the same class of bug as the router's own oldest-20 history slice.
  getMessages: (sessionId: number) =>
    request<Message[]>(`/api/chat/sessions/${sessionId}/messages?order=desc&limit=200`),
};

/** Everything the backend sends on the terminal `done` event. */
export interface DoneMeta {
  citationMeta: CitationMeta;
  retrieval: RetrievalMeta;
}

// Streaming message send.
//
// The trailing arguments are an object, not more positional parameters. Four
// optional `string | undefined` parameters in a row are freely interchangeable
// to the type system, and the body was previously `any`, so inserting one more
// would have silently misrouted a setting into the wrong field with nothing to
// catch it.
export function sendMessageStream(
  sessionId: number,
  content: string,
  onToken: (token: string) => void,
  onDone: (sources: Message["sources"], meta: DoneMeta) => void,
  onError: (err: string) => void,
  opts: SendOptions = {}
): void {
  const token = getToken();
  const body: MessageBody = { content };
  if (opts.apiKey) body.api_key = opts.apiKey;
  if (opts.model) body.model = opts.model;
  if (opts.provider) body.provider = opts.provider;
  if (opts.baseUrl) body.base_url = opts.baseUrl;
  // Only ever sent when non-empty. The backend treats an empty array as "no
  // scope", so sending one while the composer still shows a scope chip would
  // mean the chip was lying.
  if (opts.paperIds && opts.paperIds.length > 0) body.paper_ids = opts.paperIds;

  // Whether the stream delivered its terminal event. The composer is only
  // re-enabled from onDone/onError, so a stream that dies without one used to
  // leave it disabled until the page was reloaded.
  let sawDone = false;

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
            else if (event.type === "done") {
              sawDone = true;
              // A failure inside the server-side generator still arrives as a
              // terminal `done` carrying the error, so the stream always has
              // exactly one way out.
              if (event.error) {
                onError(event.error);
                return;
              }
              // Every key defaults, so a payload from an older backend (or a
              // deployment mid-upgrade) is harmless rather than a crash.
              const rawScope = event.scope ?? {};
              onDone(event.sources ?? [], {
                citationMeta: {
                  cited: event.cited ?? [],
                  unresolved: event.unresolved ?? [],
                },
                retrieval: {
                  // Normalised key by key, not just defaulted as a whole: a
                  // partial payload such as `{applied: true}` would otherwise
                  // reach the renderer with no `paper_titles` on it.
                  scope: {
                    applied: rawScope.applied === true,
                    paper_ids: Array.isArray(rawScope.paper_ids) ? rawScope.paper_ids : [],
                    paper_titles: Array.isArray(rawScope.paper_titles)
                      ? rawScope.paper_titles
                      : [],
                  },
                  coverage: event.coverage ?? null,
                  coverageLine: typeof event.coverage_line === "string"
                    ? event.coverage_line
                    : null,
                  abstained: event.abstained === true,
                },
              });
            }
          } catch {
            // ignore malformed SSE lines
          }
        }
      }

      if (!sawDone) {
        onError("The connection closed before the answer finished.");
      }
    })
    .catch((err) => onError(err.message));
}

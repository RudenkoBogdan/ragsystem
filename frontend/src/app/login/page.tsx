"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { authApi } from "@/lib/api";
import { setToken } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const fn = mode === "login" ? authApi.login : authApi.register;
      const { access_token } = await fn(username, password);
      setToken(access_token);
      router.push("/chat");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-bg-primary">
      <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-8 shadow-2xl">
        <div className="mb-8 text-center">
          <div className="mb-2 text-3xl">🔬</div>
          <h1 className="text-xl font-semibold text-text-primary">Research Assistant</h1>
          <p className="mt-1 text-sm text-text-secondary">RAG-powered scientific chat</p>
        </div>

        <div className="mb-6 flex rounded-lg bg-bg-tertiary p-1">
          {(["login", "register"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`flex-1 rounded-md py-1.5 text-sm font-medium transition-colors ${
                mode === m
                  ? "bg-bg-secondary text-text-primary shadow"
                  : "text-text-secondary hover:text-text-primary"
              }`}
            >
              {m === "login" ? "Sign In" : "Register"}
            </button>
          ))}
        </div>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm text-text-secondary">Username</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
              className="w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-blue transition-colors"
              placeholder="Enter username"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm text-text-secondary">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-blue transition-colors"
              placeholder="Enter password"
            />
          </div>

          {error && (
            <p className="rounded-lg bg-red-900/30 border border-red-800 px-3 py-2 text-sm text-red-400">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-accent-blue py-2.5 text-sm font-medium text-white hover:bg-accent-blue-hover disabled:opacity-50 transition-colors"
          >
            {loading ? "Please wait..." : mode === "login" ? "Sign In" : "Create Account"}
          </button>
        </form>
      </div>
    </div>
  );
}

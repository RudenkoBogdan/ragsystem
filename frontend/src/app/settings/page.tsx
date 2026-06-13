"use client";
import { useRouter } from "next/navigation";
import { ArrowLeft, Save, RefreshCw } from "lucide-react";
import { useState, useEffect } from "react";

type Provider = "openrouter" | "ollama";

const OPENROUTER_MODELS = [
  "anthropic/claude-3-5-sonnet",
  "openai/gpt-4o",
  "openai/gpt-4-turbo",
  "meta-llama/llama-3.1-70b-instruct",
  "google/gemini-flash-1.5",
];

const DEFAULT_OLLAMA_URL = "http://localhost:11434/v1";

export default function Settings() {
  const router = useRouter();
  const [provider, setProvider] = useState<Provider>("openrouter");

  // OpenRouter
  const [model, setModel] = useState("anthropic/claude-3-5-sonnet");
  const [apiKey, setApiKey] = useState("");

  // Ollama
  const [ollamaUrl, setOllamaUrl] = useState(DEFAULT_OLLAMA_URL);
  const [ollamaModel, setOllamaModel] = useState("llama3");
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [fetchError, setFetchError] = useState("");

  const [isSaved, setIsSaved] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    setProvider((localStorage.getItem("llm_provider") as Provider) || "openrouter");
    setModel(localStorage.getItem("openrouter_model") || "anthropic/claude-3-5-sonnet");
    setApiKey(localStorage.getItem("openrouter_api_key") || "");
    setOllamaUrl(localStorage.getItem("ollama_base_url") || DEFAULT_OLLAMA_URL);
    setOllamaModel(localStorage.getItem("ollama_model") || "llama3");
    setIsLoading(false);
  }, []);

  // Fetch installed Ollama models from the local daemon (browser → host).
  // Strips the /v1 suffix to hit Ollama's native /api/tags endpoint.
  const fetchOllamaModels = async () => {
    setFetchingModels(true);
    setFetchError("");
    try {
      const root = ollamaUrl.replace(/\/v1\/?$/, "");
      const res = await fetch(`${root}/api/tags`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const names = (data.models || []).map((m: any) => m.name);
      setOllamaModels(names);
      if (names.length > 0 && !names.includes(ollamaModel)) {
        setOllamaModel(names[0]);
      }
    } catch (e: any) {
      setFetchError(
        `Не удалось получить список моделей (${e.message}). Убедитесь, что Ollama запущена и доступна по этому адресу.`
      );
    } finally {
      setFetchingModels(false);
    }
  };

  const handleSave = () => {
    localStorage.setItem("llm_provider", provider);
    localStorage.setItem("openrouter_model", model);
    if (apiKey) localStorage.setItem("openrouter_api_key", apiKey);
    localStorage.setItem("ollama_base_url", ollamaUrl);
    localStorage.setItem("ollama_model", ollamaModel);
    setIsSaved(true);
    setTimeout(() => setIsSaved(false), 2000);
  };

  const handleReset = () => {
    ["llm_provider", "openrouter_model", "openrouter_api_key", "ollama_base_url", "ollama_model"].forEach(
      (k) => localStorage.removeItem(k)
    );
    setProvider("openrouter");
    setModel("anthropic/claude-3-5-sonnet");
    setApiKey("");
    setOllamaUrl(DEFAULT_OLLAMA_URL);
    setOllamaModel("llama3");
    setOllamaModels([]);
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg-primary flex items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-blue border-t-transparent" />
      </div>
    );
  }

  const inputClass =
    "w-full px-3 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary placeholder-text-muted focus:outline-none focus:border-accent-blue transition-colors";

  return (
    <div className="min-h-screen bg-bg-primary">
      {/* Header */}
      <div className="border-b border-border bg-bg-secondary">
        <div className="max-w-2xl mx-auto px-4 py-4 flex items-center gap-4">
          <button
            onClick={() => router.back()}
            className="p-2 hover:bg-bg-tertiary rounded-lg transition-colors"
          >
            <ArrowLeft className="h-5 w-5 text-text-secondary" />
          </button>
          <h1 className="text-xl font-semibold text-text-primary">Settings</h1>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-2xl mx-auto px-4 py-8">
        <div className="space-y-8">
          {/* Provider selection */}
          <div>
            <h2 className="text-lg font-semibold text-text-primary mb-4">Provider</h2>
            <div className="grid grid-cols-2 gap-3">
              {([
                { id: "openrouter", label: "OpenRouter", desc: "Облачные модели по API-ключу" },
                { id: "ollama", label: "Ollama (локально)", desc: "Локальные модели на вашей машине" },
              ] as const).map((p) => (
                <button
                  key={p.id}
                  onClick={() => setProvider(p.id)}
                  className={`text-left p-4 rounded-lg border transition-colors ${
                    provider === p.id
                      ? "border-accent-blue bg-accent-blue/10"
                      : "border-border bg-bg-secondary hover:bg-bg-tertiary"
                  }`}
                >
                  <div className="font-medium text-text-primary">{p.label}</div>
                  <div className="text-xs text-text-muted mt-1">{p.desc}</div>
                </button>
              ))}
            </div>
          </div>

          {/* OpenRouter settings */}
          {provider === "openrouter" && (
            <>
              <div>
                <h2 className="text-lg font-semibold text-text-primary mb-4">Model Configuration</h2>
                <label className="block text-sm font-medium text-text-primary mb-2">LLM Model</label>
                <input
                  list="openrouter-models"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className={inputClass}
                  placeholder="anthropic/claude-3-5-sonnet"
                />
                <datalist id="openrouter-models">
                  {OPENROUTER_MODELS.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                <p className="mt-2 text-xs text-text-muted">
                  Выберите или введите имя модели OpenRouter.
                </p>
              </div>

              <div>
                <h2 className="text-lg font-semibold text-text-primary mb-4">OpenRouter API Key</h2>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-or-v1-..."
                  className={inputClass}
                />
                <p className="mt-2 text-xs text-text-muted">
                  Оставьте пустым, чтобы использовать серверный ключ по умолчанию. Ключ хранится только локально.
                </p>
              </div>
            </>
          )}

          {/* Ollama settings */}
          {provider === "ollama" && (
            <>
              <div>
                <h2 className="text-lg font-semibold text-text-primary mb-4">Ollama Server</h2>
                <label className="block text-sm font-medium text-text-primary mb-2">Base URL</label>
                <input
                  value={ollamaUrl}
                  onChange={(e) => setOllamaUrl(e.target.value)}
                  placeholder={DEFAULT_OLLAMA_URL}
                  className={inputClass}
                />
                <p className="mt-2 text-xs text-text-muted">
                  Адрес OpenAI-совместимого эндпоинта Ollama. По умолчанию: {DEFAULT_OLLAMA_URL}.
                  В Docker localhost автоматически заменяется на host.docker.internal.
                </p>
              </div>

              <div>
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-lg font-semibold text-text-primary">Model</h2>
                  <button
                    onClick={fetchOllamaModels}
                    disabled={fetchingModels}
                    className="flex items-center gap-2 px-3 py-1.5 text-xs rounded-lg border border-border text-text-secondary hover:bg-bg-secondary transition-colors disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${fetchingModels ? "animate-spin" : ""}`} />
                    Обновить список
                  </button>
                </div>
                <input
                  list="ollama-models"
                  value={ollamaModel}
                  onChange={(e) => setOllamaModel(e.target.value)}
                  placeholder="llama3"
                  className={inputClass}
                />
                <datalist id="ollama-models">
                  {ollamaModels.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                <p className="mt-2 text-xs text-text-muted">
                  Имя установленной модели (например, llama3, qwen2.5, mistral). Нажмите «Обновить список»,
                  чтобы подтянуть модели из Ollama.
                </p>
                {fetchError && (
                  <p className="mt-2 text-xs text-red-400">{fetchError}</p>
                )}
              </div>
            </>
          )}

          {/* Actions */}
          <div className="flex gap-3 pt-4 border-t border-border">
            <button
              onClick={handleSave}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-blue text-white font-medium hover:opacity-90 transition-opacity"
            >
              <Save className="h-4 w-4" />
              Save Settings
            </button>
            <button
              onClick={handleReset}
              className="flex items-center gap-2 px-4 py-2 rounded-lg border border-border text-text-secondary hover:bg-bg-secondary transition-colors"
            >
              Reset to Defaults
            </button>
          </div>

          {isSaved && (
            <div className="p-3 rounded-lg bg-green-500/10 border border-green-500/50 text-green-400 text-sm">
              ✓ Settings saved successfully
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

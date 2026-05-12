"use client";
import { useRouter } from "next/navigation";
import { ArrowLeft, Save } from "lucide-react";
import { useState, useEffect } from "react";

const MODELS = [
  "gpt-4o",
  "gpt-4-turbo",
  "gpt-3.5-turbo",
  "meta-llama/llama-3.1-70b-instruct",
  "meta-llama/llama-2-70b-chat",
];

export default function Settings() {
  const router = useRouter();
  const [model, setModel] = useState("gpt-4o");
  const [apiKey, setApiKey] = useState("");
  const [isSaved, setIsSaved] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const savedModel = localStorage.getItem("openrouter_model") || "gpt-4o";
    const savedKey = localStorage.getItem("openrouter_api_key") || "";
    setModel(savedModel);
    setApiKey(savedKey);
    setIsLoading(false);
  }, []);

  const handleSave = () => {
    localStorage.setItem("openrouter_model", model);
    if (apiKey) {
      localStorage.setItem("openrouter_api_key", apiKey);
    }
    setIsSaved(true);
    setTimeout(() => setIsSaved(false), 2000);
  };

  const handleReset = () => {
    localStorage.removeItem("openrouter_model");
    localStorage.removeItem("openrouter_api_key");
    setModel("gpt-4o");
    setApiKey("");
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg-primary flex items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-blue border-t-transparent" />
      </div>
    );
  }

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
          {/* Model Selection */}
          <div>
            <h2 className="text-lg font-semibold text-text-primary mb-4">Model Configuration</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-text-primary mb-2">
                  LLM Model
                </label>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary focus:outline-none focus:border-accent-blue transition-colors"
                >
                  {MODELS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                <p className="mt-2 text-xs text-text-muted">
                  Select which OpenRouter model to use for AI responses
                </p>
              </div>
            </div>
          </div>

          {/* API Key */}
          <div>
            <h2 className="text-lg font-semibold text-text-primary mb-4">OpenRouter API Key</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-text-primary mb-2">
                  API Key
                </label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-or-v1-..."
                  className="w-full px-3 py-2 rounded-lg bg-bg-secondary border border-border text-text-primary placeholder-text-muted focus:outline-none focus:border-accent-blue transition-colors"
                />
                <p className="mt-2 text-xs text-text-muted">
                  Leave empty to use the default server API key. Your custom key is stored locally only.
                </p>
              </div>
            </div>
          </div>

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

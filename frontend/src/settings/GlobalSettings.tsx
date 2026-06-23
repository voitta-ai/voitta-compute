// Global tab — provider, API key, model, layout, theme.
//
// Extracted from the original single-tab SettingsView. Identical
// behaviour: per-provider api_keys + models so flipping providers
// preserves the saved key of the previous one (it reappears as a
// ``●●●● (saved)`` placeholder).

import { useEffect, useState } from "react";
import {
  AGENT_SDK_PROVIDER,
  bootstrapSettings,
  saveSettings,
  type Layout,
  type ProviderId,
  type SettingsPatch,
  type Theme,
} from "../lib/settings";
import { useSettings } from "../lib/useSettings";

const PROVIDERS: { id: ProviderId; label: string }[] = [
  { id: "anthropic", label: "Anthropic (Claude)" },
  { id: "openai", label: "OpenAI (ChatGPT)" },
  { id: "gemini", label: "Google (Gemini)" },
];

const MODEL_CHOICES: Record<ProviderId, string[]> = {
  anthropic: [
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
  ],
  openai: ["gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-5"],
  gemini: [
    "gemini-2.0-flash-exp",
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
  ],
  claude_code: ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6"],
};

const KEY_PLACEHOLDER: Record<ProviderId, string> = {
  anthropic: "sk-ant-...",
  openai: "sk-...",
  gemini: "AIza...",
  claude_code: "", // subscription brain — no API key field
};

interface Props {
  backendOrigin: string;
}

export default function GlobalSettings({ backendOrigin }: Props) {
  const cached = useSettings();
  const [provider, setProvider] = useState<ProviderId>(cached.provider);
  const [models, setModels] = useState<Record<string, string>>(cached.models);
  const [layout, setLayout] = useState<Layout>(cached.layout);
  const [theme, setTheme] = useState<Theme>(cached.theme);
  const [maxToolIters, setMaxToolIters] = useState<number>(cached.max_tool_iterations);
  const [maxTokens, setMaxTokens] = useState<number>(cached.max_tokens);
  const [apiKey, setApiKey] = useState("");
  const [hasKey, setHasKey] = useState<Record<string, boolean>>(cached.has_api_keys);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ text: string; err: boolean } | null>(null);

  // Clear the in-flight unsaved key when switching providers so a
  // half-pasted key for OpenAI doesn't end up saved under Anthropic.
  useEffect(() => setApiKey(""), [provider]);

  useEffect(() => {
    setProvider(cached.provider);
    setModels(cached.models);
    setLayout(cached.layout);
    setTheme(cached.theme);
    setMaxToolIters(cached.max_tool_iterations);
    setMaxTokens(cached.max_tokens);
    setHasKey(cached.has_api_keys);
  }, [cached]);

  async function onSave() {
    setSaving(true);
    setStatus(null);
    const patch: SettingsPatch = {
      provider, layout, theme,
      max_tool_iterations: maxToolIters,
      max_tokens: maxTokens,
    };
    if (models[provider]) patch.models = { [provider]: models[provider] };
    if (apiKey) patch.api_keys = { [provider]: apiKey };
    try {
      await saveSettings(backendOrigin, patch);
      setApiKey("");
      setStatus({ text: "Saved.", err: false });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  async function onClearKey() {
    setSaving(true);
    try {
      await saveSettings(backendOrigin, { api_keys: { [provider]: "" } });
      setStatus({ text: "Key cleared.", err: false });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  const choices = MODEL_CHOICES[provider] ?? [];
  const model = models[provider] ?? "";
  const providerHasKey = Boolean(hasKey[provider]);
  const isBrain = provider === AGENT_SDK_PROVIDER;
  // The subscription brain is only offered when the Claude Code engine is
  // installed on this machine (Phase 4 gating).
  const providerOptions = cached.agent_sdk.available
    ? [...PROVIDERS, { id: AGENT_SDK_PROVIDER, label: "Claude (subscription)" }]
    : PROVIDERS;

  async function onDisconnectToken() {
    setSaving(true);
    try {
      await fetch(`${backendOrigin}/api/agent_sdk/disconnect`, {
        method: "POST",
        credentials: "include",
      });
      await bootstrapSettings(backendOrigin); // refresh has_token
      setStatus({ text: "Disconnected.", err: false });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="view-settings global-settings">
      <label htmlFor="vb-provider">Provider</label>
      <select
        id="vb-provider"
        value={provider}
        onChange={(e) => setProvider(e.target.value as ProviderId)}
      >
        {providerOptions.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
            {p.id === AGENT_SDK_PROVIDER
              ? cached.agent_sdk.has_token
                ? " ✓"
                : ""
              : hasKey[p.id]
                ? " ✓"
                : ""}
          </option>
        ))}
      </select>
      <p className="muted">You can keep keys for all providers and switch any time.</p>

      {isBrain ? (
        // Subscription brain: no API key. Auth is a one-time token collected
        // in chat (claude setup-token) the first time you send a message.
        <div className="agent-sdk-auth" style={{ marginTop: 4 }}>
          {cached.agent_sdk.has_token ? (
            <div className="muted">
              ✓ Claude subscription connected.{" "}
              <button
                type="button"
                className="link-btn"
                onClick={onDisconnectToken}
                disabled={saving}
              >
                Disconnect
              </button>
              .
            </div>
          ) : (
            <p className="muted">
              No API key needed — uses your Claude Pro/Max subscription. The
              first time you send a message I'll walk you through pasting a
              one-time token (<code>claude setup-token</code>). It's stored
              locally and never shown in chat.
            </p>
          )}
        </div>
      ) : (
        <>
          <label htmlFor="vb-key">API key</label>
          <input
            id="vb-key"
            type="text"
            className="secret"
            value={apiKey}
            placeholder={
              providerHasKey
                ? "●●●●●●●●●●●●●●●●  (saved — type to replace)"
                : KEY_PLACEHOLDER[provider]
            }
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          {providerHasKey && (
            <div className="muted">
              A key for {provider} is saved on disk.{" "}
              <button
                type="button"
                className="link-btn"
                onClick={onClearKey}
                disabled={saving}
              >
                Clear it
              </button>
              .
            </div>
          )}
        </>
      )}

      <label htmlFor="vb-model">Model</label>
      <select
        id="vb-model"
        value={choices.includes(model) ? model : "_custom"}
        onChange={(e) => setModels({ ...models, [provider]: e.target.value })}
      >
        {choices.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
        {!choices.includes(model) && model && (
          <option value="_custom">custom: {model}</option>
        )}
      </select>

      <label>Layout</label>
      <div className="radio-row">
        <label className="radio">
          <input
            type="radio"
            name="layout"
            checked={layout === "chat-right"}
            onChange={() => setLayout("chat-right")}
          />
          chat-right
        </label>
        <label className="radio">
          <input
            type="radio"
            name="layout"
            checked={layout === "chat-left"}
            onChange={() => setLayout("chat-left")}
          />
          chat-left
        </label>
      </div>

      <label htmlFor="vb-theme">Theme</label>
      <select
        id="vb-theme"
        value={theme}
        onChange={(e) => setTheme(e.target.value as Theme)}
      >
        <option value="auto">Auto (follow OS)</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>

      <label htmlFor="vb-max-tokens">Max response tokens per turn</label>
      <input
        id="vb-max-tokens"
        type="number"
        min={256}
        max={200000}
        step={1024}
        value={maxTokens}
        onChange={(e) => setMaxTokens(Math.max(256, Number(e.target.value) || 256))}
      />
      <p className="muted">
        Per-call response cap sent to the LLM as max_tokens. When the model
        hits it, the response truncates and you'll see a ⚠️ note explaining
        why. Default 24576 (~24K).
      </p>

      <label htmlFor="vb-max-tool-iters">Max tool-use iterations per turn</label>
      <input
        id="vb-max-tool-iters"
        type="number"
        min={1}
        max={200}
        value={maxToolIters}
        onChange={(e) => setMaxToolIters(Math.max(1, Number(e.target.value) || 1))}
      />
      <p className="muted">
        Hard cap on tool calls the agent can chain in a single turn before
        the loop aborts. Raise if you see ⚠️ tool-use loop exceeded warnings.
      </p>

      <div className="actions">
        <button
          className="save-btn"
          type="button"
          onClick={onSave}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {status && (
          <span className={`status ${status.err ? "err" : ""}`}>{status.text}</span>
        )}
      </div>
    </div>
  );
}

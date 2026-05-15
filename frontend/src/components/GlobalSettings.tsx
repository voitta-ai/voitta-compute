// Global settings tab — the core (non-plugin) fields.
//
// This is what used to live inline in SettingsView.tsx. The Save button
// belongs to the parent so the user gets one Save action across the
// entire panel; we only export the form body and the dirty-check.

import { useEffect, useMemo, useState } from "preact/hooks";
import { fetchProviderModels, type ProviderModel } from "../lib/api";
import {
  DEFAULT_MODEL,
  MODELS_BY_PROVIDER,
  type ProviderId,
  type Settings,
} from "../lib/settings";

interface Props {
  backendOrigin: string;
  draft: Settings;
  patch: (p: Partial<Settings>) => void;
}

const PROVIDER_LABEL: Record<ProviderId, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI (ChatGPT)",
  gemini: "Google (Gemini)",
};

const KEY_FIELD: Record<ProviderId, keyof Settings> = {
  anthropic: "anthropicApiKey",
  openai: "openaiApiKey",
  gemini: "geminiApiKey",
};

const MODEL_FIELD: Record<ProviderId, keyof Settings> = {
  anthropic: "anthropicModel",
  openai: "openaiModel",
  gemini: "geminiModel",
};

const KEY_PLACEHOLDER: Record<ProviderId, string> = {
  anthropic: "sk-ant-...",
  openai: "sk-...",
  gemini: "AIza...",
};

const KEY_DEST_HINT: Record<ProviderId, string> = {
  anthropic: "Sent to api.anthropic.com (via the local backend; not stored on disk).",
  openai: "Sent to api.openai.com (via the local backend; not stored on disk).",
  gemini: "Sent to generativelanguage.googleapis.com (via the local backend; not stored on disk).",
};

export function GlobalSettings({ backendOrigin, draft, patch }: Props) {
  const [liveModels, setLiveModels] = useState<Record<ProviderId, ProviderModel[] | null>>({
    anthropic: null,
    openai: null,
    gemini: null,
  });
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  const activeKey = String(draft[KEY_FIELD[draft.provider]] ?? "").trim();
  useEffect(() => {
    if (!activeKey) {
      setModelsError(null);
      setModelsLoading(false);
      return;
    }
    if (liveModels[draft.provider] !== null) return;
    const ctrl = new AbortController();
    setModelsLoading(true);
    setModelsError(null);
    fetchProviderModels(backendOrigin, draft.provider, activeKey, ctrl.signal)
      .then((models) => {
        setLiveModels((prev) => ({ ...prev, [draft.provider]: models }));
        setModelsLoading(false);
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        const msg = err instanceof Error ? err.message : String(err);
        setModelsError(msg);
        setModelsLoading(false);
      });
    return () => ctrl.abort();
  }, [draft.provider, activeKey, backendOrigin, liveModels]);

  function refreshModels() {
    setLiveModels((prev) => ({ ...prev, [draft.provider]: null }));
  }

  function onProviderChange(p: ProviderId) {
    patch({
      provider: p,
      [MODEL_FIELD[p]]: pickModel(draft, p),
    } as Partial<Settings>);
  }

  const providerKey = String(draft[KEY_FIELD[draft.provider]] ?? "");
  const providerModel = String(draft[MODEL_FIELD[draft.provider]] ?? "");
  const live = liveModels[draft.provider];
  const modelOptions = live ? live.map((m) => m.id) : MODELS_BY_PROVIDER[draft.provider];

  return (
    <div class="global-settings">
      <label htmlFor="provider">Provider</label>
      <select
        id="provider"
        value={draft.provider}
        onChange={(e) =>
          onProviderChange((e.currentTarget as HTMLSelectElement).value as ProviderId)
        }
      >
        {(Object.keys(PROVIDER_LABEL) as ProviderId[]).map((p) => (
          <option key={p} value={p}>
            {PROVIDER_LABEL[p]}
          </option>
        ))}
      </select>
      <p class="muted">You can keep keys for all three providers and switch any time.</p>

      {(Object.keys(KEY_FIELD) as ProviderId[]).map((p) => (
        <div key={p}>
          <label htmlFor={`key-${p}`}>{PROVIDER_LABEL[p]} API key</label>
          <input
            id={`key-${p}`}
            class="secret"
            type="text"
            spellcheck={false}
            autocomplete="off"
            autocorrect="off"
            autocapitalize="off"
            data-lpignore="true"
            data-1p-ignore="true"
            data-form-type="other"
            placeholder={KEY_PLACEHOLDER[p]}
            value={String(draft[KEY_FIELD[p]] ?? "")}
            onInput={(e) =>
              patch({
                [KEY_FIELD[p]]: (e.currentTarget as HTMLInputElement).value,
              } as Partial<Settings>)
            }
          />
          <p class="muted">{KEY_DEST_HINT[p]}</p>
        </div>
      ))}

      <label htmlFor="model">Model</label>
      <select
        id="model"
        value={providerModel}
        onChange={(e) =>
          patch({
            [MODEL_FIELD[draft.provider]]: (e.currentTarget as HTMLSelectElement).value,
          } as Partial<Settings>)
        }
      >
        {modelOptions.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
        {!modelOptions.includes(providerModel) && providerModel && (
          <option value={providerModel}>{providerModel} (custom)</option>
        )}
      </select>
      <p class="muted">
        {modelsLoading && "Loading models from provider…"}
        {!modelsLoading && modelsError && (
          <>
            Couldn't fetch live model list ({modelsError}); showing the bundled
            list. <a href="#" onClick={(e) => { e.preventDefault(); refreshModels(); }}>Retry</a>
          </>
        )}
        {!modelsLoading && !modelsError && liveModels[draft.provider] && (
          <>
            Live from provider ({liveModels[draft.provider]?.length} models).{" "}
            <a href="#" onClick={(e) => { e.preventDefault(); refreshModels(); }}>Refresh</a>
          </>
        )}
        {!modelsLoading && !modelsError && !liveModels[draft.provider] && !providerKey &&
          "Add an API key above to load the live model list."}
      </p>

      <label htmlFor="maxTokens">Max tokens per response</label>
      <input
        id="maxTokens"
        type="number"
        min={256}
        max={65536}
        value={draft.maxTokens}
        onInput={(e) =>
          patch({
            maxTokens: parseInt((e.currentTarget as HTMLInputElement).value, 10) || 0,
          })
        }
      />

      <label htmlFor="maxToolIterations">Max tool-use iterations per turn</label>
      <input
        id="maxToolIterations"
        type="number"
        min={1}
        max={200}
        value={draft.maxToolIterations}
        onInput={(e) =>
          patch({
            maxToolIterations:
              parseInt((e.currentTarget as HTMLInputElement).value, 10) || 0,
          })
        }
      />
      <p class="muted">
        How many tool-call rounds the model may chain before the loop is cut off. Default 25;
        server hard ceiling is 200.
      </p>

      <label
        style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "12px" }}
      >
        <input
          type="checkbox"
          checked={!!draft.jsCompute}
          onChange={(e) =>
            patch({ jsCompute: (e.currentTarget as HTMLInputElement).checked })
          }
        />
        <span>JS compute (browser-side buffers + Chart.js + buffer_eval)</span>
      </label>
      <p class="muted">
        Off by default — the standard workflow is Python-only:
        <code>download_to_python_storage</code> → compute scripts via{" "}
        <code>run_compute</code>. Enable to expose the legacy
        <code>fetch_to_buffer</code> / <code>parse_file_to_buffer</code> /{" "}
        <code>plot_*</code> / <code>buffer_eval</code> tool family that runs in
        the browser. Both paradigms work, but enabling both at once gives the
        LLM two ways to do the same thing — confuses tool selection.
      </p>

      <label
        style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "12px" }}
      >
        <input
          type="checkbox"
          checked={draft.webFetch !== false}
          onChange={(e) =>
            patch({ webFetch: (e.currentTarget as HTMLInputElement).checked })
          }
        />
        <span>Web retrieval (<code>web_fetch</code>)</span>
      </label>
      <p class="muted">
        On by default. Lets the LLM GET a URL on the open web and read the
        page as text — articles, docs, JSON, or PDF. Requests go out from the
        local backend with browser-shaped headers and a persistent cookie
        jar; TLS verification stays on, no JavaScript executes. Turn off to
        hide the tool from the LLM entirely.
      </p>

      <label style={{ marginTop: "16px", display: "block" }}>Panel layout</label>
      <div style={{ display: "flex", gap: "8px", marginTop: "6px" }}>
        {(["chat-right", "chat-left"] as const).map((opt) => (
          <label
            key={opt}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              cursor: "pointer",
              padding: "5px 12px",
              border: "1px solid var(--voitta-border)",
              borderRadius: "6px",
              background: draft.layout === opt ? "var(--voitta-accent-tint)" : "transparent",
              fontWeight: draft.layout === opt ? 600 : 400,
            }}
          >
            <input
              type="radio"
              name="layout"
              value={opt}
              checked={draft.layout === opt}
              onChange={() => patch({ layout: opt })}
              style={{ accentColor: "var(--voitta-accent)" }}
            />
            {opt === "chat-right" ? "Chat right, report left" : "Chat left, report right"}
          </label>
        ))}
      </div>
      <p class="muted">
        Default: chat on the right, report on the left. Changes take effect immediately after Save.
      </p>

      {!providerKey && (
        <p class="status err" style={{ marginTop: "16px" }}>
          No key set for {PROVIDER_LABEL[draft.provider]}.
        </p>
      )}
    </div>
  );
}

function pickModel(s: Settings, p: ProviderId): string {
  const cur = String(s[MODEL_FIELD[p]] ?? "");
  if (cur && MODELS_BY_PROVIDER[p].includes(cur)) return cur;
  return DEFAULT_MODEL[p];
}

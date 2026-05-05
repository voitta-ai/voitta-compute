// In-pane settings view. Mirrors the original plugin's panel: provider
// selector, per-provider API keys (visually masked but `type="text"` so
// Chrome doesn't pop the "Save password?" prompt), per-provider model
// dropdown, max tokens, max tool iterations.
//
// Settings persist in localStorage via lib/settings.ts; the Save button
// is the only path that writes. We DON'T auto-save on each keystroke
// because key fields are sensitive and accidental partial writes are
// confusing.

import { useEffect, useMemo, useState } from "preact/hooks";
import { fetchProviderModels, type ProviderModel } from "../lib/api";
import {
  DEFAULT_MODEL,
  MODELS_BY_PROVIDER,
  type ProviderId,
  type Settings,
  loadSettings,
  saveSettings,
} from "../lib/settings";

interface Props {
  backendOrigin: string;
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

export function SettingsView({ backendOrigin }: Props) {
  const [draft, setDraft] = useState<Settings>(() => loadSettings());
  const [status, setStatus] = useState<{ text: string; isError?: boolean } | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState<Settings>(() => loadSettings());

  // Live models per-provider, fetched from /api/providers/models. `null`
  // means "not yet fetched / fetch failed" — falls back to the hardcoded
  // MODELS_BY_PROVIDER catalog so a bad key never locks the dropdown.
  const [liveModels, setLiveModels] = useState<Record<ProviderId, ProviderModel[] | null>>({
    anthropic: null,
    openai: null,
    gemini: null,
  });
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Restore the saved snapshot on mount in case the user opened settings
  // after editing somewhere else.
  useEffect(() => {
    const s = loadSettings();
    setDraft(s);
    setSavedSnapshot(s);
  }, []);

  // Pull live models when the provider changes (or the panel opens with
  // a key already present). We re-fetch on key change too so the user
  // gets fresh data after pasting a new key.
  const activeKey = String(draft[KEY_FIELD[draft.provider]] ?? "").trim();
  useEffect(() => {
    if (!activeKey) {
      setModelsError(null);
      setModelsLoading(false);
      return;
    }
    if (liveModels[draft.provider] !== null) {
      // Already have a cached fetch for this provider in this session.
      return;
    }
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
    // Drop the cache for the active provider; the effect above fires again.
    setLiveModels((prev) => ({ ...prev, [draft.provider]: null }));
  }

  const dirty = useMemo(() => !shallowEqual(draft, savedSnapshot), [draft, savedSnapshot]);

  function patch(p: Partial<Settings>) {
    setDraft((d) => ({ ...d, ...p }));
  }

  function onProviderChange(p: ProviderId) {
    patch({
      provider: p,
      // If the per-provider model is missing or no longer in the catalogue,
      // fall back to the provider's default.
      [MODEL_FIELD[p]]: pickModel(draft, p),
    } as Partial<Settings>);
  }

  function onSave() {
    const next = saveSettings(draft);
    setSavedSnapshot(next);
    setDraft(next);
    setStatus({ text: "Saved." });
    setTimeout(() => setStatus(null), 2200);
  }

  const providerKey = String(draft[KEY_FIELD[draft.provider]] ?? "");
  const providerModel = String(draft[MODEL_FIELD[draft.provider]] ?? "");

  // Prefer the live model list when we have one; fall back to the bundled
  // catalog so the dropdown always has options even before the first fetch.
  const live = liveModels[draft.provider];
  const modelOptions = live ? live.map((m) => m.id) : MODELS_BY_PROVIDER[draft.provider];

  return (
    <section class="view view-settings">
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
        {/* If the saved model isn't in the live/cached catalogue, keep it
            visible so the user doesn't get auto-downgraded silently. */}
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

      <GoogleDriveSection backendOrigin={backendOrigin} />

      <p class="scope">
        Stored on the local backend at <code>~/.config/voitta-bookmarklet/settings.json</code> —
        shared across every host the bookmarklet runs on.
      </p>

      <div class="actions">
        <button
          class="save-btn"
          type="button"
          disabled={!dirty}
          onClick={onSave}
          title={dirty ? "Save changes" : "Nothing to save"}
        >
          Save
        </button>
        {!providerKey && (
          <span class="status err">No key set for {PROVIDER_LABEL[draft.provider]}.</span>
        )}
        {status && (
          <span class={`status${status.isError ? " err" : ""}`} role="status" aria-live="polite">
            {status.text}
          </span>
        )}
      </div>
    </section>
  );
}

function shallowEqual<T extends Record<string, unknown>>(a: T, b: T): boolean {
  const keys = Object.keys(a) as (keyof T)[];
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => a[k] === b[k]);
}

function pickModel(s: Settings, p: ProviderId): string {
  const cur = String(s[MODEL_FIELD[p]] ?? "");
  if (cur && MODELS_BY_PROVIDER[p].includes(cur)) return cur;
  return DEFAULT_MODEL[p];
}


// ---- Google Drive (OAuth) section ---------------------------------------

interface GoogleStatus {
  configured: boolean;
  connected: boolean;
  account_email?: string;
  scopes?: string[];
  expires_in_s?: number;
}

function GoogleDriveSection({ backendOrigin }: { backendOrigin: string }) {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [configureOpen, setConfigureOpen] = useState(false);

  async function refresh() {
    try {
      const r = await fetch(`${backendOrigin}/api/google/status`, {
        credentials: "omit",
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      setStatus((await r.json()) as GoogleStatus);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    void refresh();
    // Re-check periodically if a popup is open. Quiet otherwise.
    const id = window.setInterval(() => {
      if (polling) void refresh();
    }, 2000);
    return () => window.clearInterval(id);
  }, [polling]);

  function connect() {
    // Opens the OAuth start URL in a popup. Backend redirects to Google;
    // Google redirects back to /api/google/oauth/callback which closes
    // the window. We poll /api/google/status until `connected: true` or
    // the polling window expires.
    setBusy(true);
    setPolling(true);
    setErr(null);
    const url = `${backendOrigin}/api/google/oauth/start`;
    const w = window.open(url, "_blank", "width=520,height=640");
    if (!w) {
      setBusy(false);
      setPolling(false);
      setErr("Popup blocked. Allow popups for this page and retry.");
      return;
    }
    // Stop polling after 90 s either way.
    const timer = window.setTimeout(() => {
      setBusy(false);
      setPolling(false);
    }, 90_000);
    // If the popup closes (user finished or cancelled), do one final
    // refresh + stop polling shortly after.
    const interval = window.setInterval(() => {
      if (w.closed) {
        window.clearInterval(interval);
        window.setTimeout(async () => {
          await refresh();
          setBusy(false);
          setPolling(false);
          window.clearTimeout(timer);
        }, 800);
      }
    }, 500);
  }

  async function disconnect() {
    if (!confirm("Disconnect Google Drive? Drive tools will be hidden from the chat.")) return;
    setBusy(true);
    try {
      const r = await fetch(`${backendOrigin}/api/google/disconnect`, {
        method: "POST",
        credentials: "omit",
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const configured = !!status?.configured;
  const connected = !!status?.connected;

  return (
    <div class="google-drive-section" style={{ marginTop: "16px", paddingTop: "12px", borderTop: "1px solid #ddd" }}>
      <h3 style={{ margin: "0 0 6px", fontSize: "14px" }}>Google Drive</h3>
      {status === null && <p class="muted">Loading…</p>}
      {status && !configured && (
        <p class="muted">
          Not configured. Click <b>Configure</b> to paste your Google OAuth
          client credentials (created in Google Cloud Console). Once
          configured, click <b>Connect</b> to sign in — the Drive tools
          then become visible to the LLM (read-only access).
        </p>
      )}
      {configured && !connected && (
        <p class="muted">
          Configured but not connected. Click <b>Connect</b> to sign in —
          the browser opens a consent popup; come back here when done.
        </p>
      )}
      {connected && (
        <p class="muted">
          Connected as <b>{status?.account_email || "(unknown)"}</b>. Drive
          tools are visible to the LLM
          {typeof status?.expires_in_s === "number"
            ? ` (token expires in ${Math.max(0, Math.round(status.expires_in_s / 60))} min — auto-refreshed)`
            : ""}
          .
        </p>
      )}

      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
        {!connected && (
          <button
            type="button"
            onClick={connect}
            disabled={busy || !configured}
            class="save-btn"
            title={!configured ? "Configure OAuth credentials first" : "Sign in with Google"}
          >
            {busy ? "Waiting for consent…" : "Connect"}
          </button>
        )}
        {connected && (
          <button
            type="button"
            onClick={disconnect}
            disabled={busy}
            class="save-btn"
          >
            Disconnect
          </button>
        )}
        <button
          type="button"
          onClick={() => setConfigureOpen((v) => !v)}
          class="save-btn"
          style={{ background: "#6b7280" }}
        >
          {configureOpen ? "Cancel" : "Configure"}
        </button>
      </div>

      {configureOpen && (
        <ConfigureForm
          backendOrigin={backendOrigin}
          connected={connected}
          onClose={async (saved) => {
            setConfigureOpen(false);
            if (saved) await refresh();
          }}
        />
      )}

      {err && (
        <p class="muted" style={{ color: "#b00020" }}>
          {err}
        </p>
      )}
    </div>
  );
}

// ---- Configure (clientId / clientSecret) inline form --------------------

interface GoogleClientConfig {
  clientId: string;
  clientSecret: string;
}

function ConfigureForm({
  backendOrigin,
  connected,
  onClose,
}: {
  backendOrigin: string;
  connected: boolean;
  onClose: (saved: boolean) => void | Promise<void>;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [pasteJson, setPasteJson] = useState("");
  const [parseErr, setParseErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  // Prefill with the last-saved values so editing one field doesn't
  // require re-typing the other.
  useEffect(() => {
    let cancelled = false;
    fetch(`${backendOrigin}/api/google/config`, { credentials: "omit" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`status ${r.status}`))))
      .then((c: GoogleClientConfig) => {
        if (cancelled) return;
        setClientId(c.clientId || "");
        setClientSecret(c.clientSecret || "");
      })
      .catch(() => {
        // Non-fatal — start with empty fields.
      });
    return () => {
      cancelled = true;
    };
  }, [backendOrigin]);

  function applyParsed(obj: unknown): boolean {
    // Google's downloadable credentials.json wraps the actual
    // credentials under either "web" or "installed" depending on the
    // OAuth client type (Web app vs Desktop). Accept both, plus a
    // flat shape for hand-built JSON.
    if (!obj || typeof obj !== "object") {
      setParseErr("Not a JSON object.");
      return false;
    }
    const o = obj as Record<string, unknown>;
    const inner =
      (o.web as Record<string, unknown> | undefined) ||
      (o.installed as Record<string, unknown> | undefined) ||
      o;
    const cid = inner.client_id;
    const csec = inner.client_secret;
    if (typeof cid !== "string" || typeof csec !== "string" || !cid || !csec) {
      setParseErr(
        "Couldn't find client_id and client_secret. Expected Google's OAuth client JSON ('web' or 'installed' shape).",
      );
      return false;
    }
    setClientId(cid);
    setClientSecret(csec);
    setParseErr(null);
    return true;
  }

  function onParseClick() {
    if (!pasteJson.trim()) {
      setParseErr("Paste the JSON first.");
      return;
    }
    try {
      applyParsed(JSON.parse(pasteJson));
    } catch (e) {
      setParseErr(`Invalid JSON: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  function onFileChange(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      setPasteJson(text);
      try {
        applyParsed(JSON.parse(text));
      } catch (err) {
        setParseErr(`Invalid JSON in file: ${err instanceof Error ? err.message : String(err)}`);
      }
    };
    reader.onerror = () => setParseErr("Couldn't read file.");
    reader.readAsText(file);
    // Allow re-selecting the same file later.
    input.value = "";
  }

  async function onSave() {
    if (!clientId.trim() || !clientSecret.trim()) {
      setSaveErr("Both client ID and client secret are required.");
      return;
    }
    if (
      connected &&
      !confirm(
        "Saving new credentials will disconnect the current Google Drive session. Continue?",
      )
    ) {
      return;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      const r = await fetch(`${backendOrigin}/api/google/configure`, {
        method: "POST",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          clientId: clientId.trim(),
          clientSecret: clientSecret.trim(),
        }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(`status ${r.status}: ${detail.slice(0, 200)}`);
      }
      await onClose(true);
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      style={{
        marginTop: "12px",
        padding: "12px",
        border: "1px solid #d1d5db",
        borderRadius: "6px",
        background: "#f9fafb",
      }}
    >
      <p class="muted" style={{ marginTop: 0 }}>
        Upload <code>credentials.json</code> from Google Cloud Console, paste
        the JSON, or fill the fields manually. The redirect URI registered
        in your OAuth client must match{" "}
        <code>https://127.0.0.1:12358/api/google/oauth/callback</code>.
      </p>

      <label>Upload JSON file</label>
      <input
        type="file"
        accept=".json,application/json"
        onChange={onFileChange}
        style={{ marginTop: "4px" }}
      />

      <label style={{ marginTop: "10px" }}>Or paste JSON</label>
      <textarea
        value={pasteJson}
        onInput={(e) => setPasteJson((e.currentTarget as HTMLTextAreaElement).value)}
        rows={4}
        spellcheck={false}
        style={{
          width: "100%",
          marginTop: "4px",
          padding: "7px 10px",
          border: "1px solid #d1d5db",
          borderRadius: "5px",
          font: "12px ui-monospace, Menlo, Consolas, monospace",
          resize: "vertical",
        }}
        placeholder='{"web": {"client_id": "...", "client_secret": "...", ...}}'
      />
      <div style={{ marginTop: "6px", display: "flex", alignItems: "center", gap: "8px" }}>
        <button
          type="button"
          onClick={onParseClick}
          class="save-btn"
          style={{ background: "#6b7280", padding: "5px 12px", fontSize: "12px" }}
        >
          Parse JSON
        </button>
        {parseErr && (
          <span class="status err" style={{ fontSize: "11px" }}>
            {parseErr}
          </span>
        )}
      </div>

      <label htmlFor="g-client-id" style={{ marginTop: "12px" }}>
        Client ID
      </label>
      <input
        id="g-client-id"
        type="text"
        spellcheck={false}
        autocomplete="off"
        data-lpignore="true"
        data-1p-ignore="true"
        value={clientId}
        onInput={(e) => setClientId((e.currentTarget as HTMLInputElement).value)}
        placeholder="...apps.googleusercontent.com"
      />

      <label htmlFor="g-client-secret">Client secret</label>
      <input
        id="g-client-secret"
        class="secret"
        type="text"
        spellcheck={false}
        autocomplete="off"
        autocorrect="off"
        autocapitalize="off"
        data-lpignore="true"
        data-1p-ignore="true"
        data-form-type="other"
        value={clientSecret}
        onInput={(e) => setClientSecret((e.currentTarget as HTMLInputElement).value)}
        placeholder="GOCSPX-..."
      />

      {connected && (
        <p class="muted" style={{ color: "#92400e", marginTop: "8px" }}>
          Saving will disconnect the current Drive session — the existing
          tokens belong to the old client.
        </p>
      )}

      <div style={{ marginTop: "12px", display: "flex", gap: "8px", alignItems: "center" }}>
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          class="save-btn"
        >
          {saving ? "Saving…" : "Save credentials"}
        </button>
        <button
          type="button"
          onClick={() => onClose(false)}
          disabled={saving}
          class="save-btn"
          style={{ background: "#6b7280" }}
        >
          Cancel
        </button>
        {saveErr && (
          <span class="status err" style={{ fontSize: "11px" }}>
            {saveErr}
          </span>
        )}
      </div>
    </div>
  );
}

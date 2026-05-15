// Google plugin — custom Settings panel.
//
// Why custom (not schema-driven): the Drive OAuth flow has a popup +
// status-poll dance that doesn't fit the declarative ``settings_schema``
// shape. The plugin manifest sets ``"settings_panel": "custom"`` and
// core SettingsView discovers this file via import.meta.glob, rendering
// it in place of the schema renderer.
//
// Two top-level concerns:
//
//   * **OAuth section** — Configure clientId/clientSecret → Connect →
//     poll /api/google/status → Disconnect. Backend persists tokens
//     in the user's keychain; this UI just kicks the flow.
//
//   * **Pickup fallback** — flat-key Settings fields
//     ``driveDownloadViaPickup`` + ``pickupDownloadsDir`` for the
//     no-OAuth racy download path. Kept as flat keys (not
//     ``plugins.google.*``) so the existing tool code that reads
//     them via /api/settings doesn't need a migration. The panel
//     itself is where they live now, conceptually.

import { useEffect, useState } from "preact/hooks";
import {
  loadSettings,
  saveSettings,
  subscribeSettings,
  type Settings,
} from "../../../frontend/src/lib/settings";

interface Props {
  pluginName: string;
  backendOrigin: string;
}

interface GoogleStatus {
  configured: boolean;
  connected: boolean;
  account_email?: string;
  scopes?: string[];
  expires_in_s?: number;
}

interface GoogleClientConfig {
  clientId: string;
  clientSecret: string;
}

export default function GoogleSettingsPanel({ backendOrigin }: Props) {
  const [snapshot, setSnapshot] = useState<Settings>(() => loadSettings());
  useEffect(() => subscribeSettings(setSnapshot), []);

  function patch(p: Partial<Settings>) {
    saveSettings(p);
  }

  return (
    <div class="plugin-settings-panel google-settings">
      <GoogleDriveSection backendOrigin={backendOrigin} />

      <hr style={{ margin: "20px 0", border: "0", borderTop: "1px solid var(--voitta-border)" }} />

      <h3 style={{ margin: "0 0 12px", fontSize: "14px" }}>No-OAuth pickup fallback</h3>
      <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <input
          type="checkbox"
          checked={!!snapshot.driveDownloadViaPickup}
          onChange={(e) =>
            patch({ driveDownloadViaPickup: (e.currentTarget as HTMLInputElement).checked })
          }
        />
        <span>Drive download via Downloads-folder pickup (no OAuth)</span>
      </label>
      <p class="muted">
        Off by default. Hacky workaround for when you don't want to set up
        Google OAuth: the LLM gets a <code>drive_pickup_to_python_storage</code>
        tool that opens the Drive download URL in a new tab (your existing
        Google session does the auth) and then watches the directory below for
        the resulting file. Visible to the LLM only when this is on AND OAuth
        is <i>not</i> connected. Racy by design — concurrent downloads can be
        misattributed.
      </p>

      <label htmlFor="pickupDownloadsDir">Downloads folder to watch</label>
      <input
        id="pickupDownloadsDir"
        type="text"
        value={snapshot.pickupDownloadsDir}
        placeholder="~/Downloads"
        onInput={(e) =>
          patch({
            pickupDownloadsDir: (e.currentTarget as HTMLInputElement).value,
          })
        }
        disabled={!snapshot.driveDownloadViaPickup}
      />
      <p class="muted">
        Default <code>~/Downloads</code>. Tilde and environment variables are
        expanded server-side. Only used when the pickup option above is enabled.
      </p>
    </div>
  );
}

// ---- OAuth section (moved verbatim from SettingsView.tsx) ----

function GoogleDriveSection({ backendOrigin }: { backendOrigin: string }) {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  const [configureOpen, setConfigureOpen] = useState(false);

  async function refresh() {
    try {
      const r = await fetch(`${backendOrigin}/api/google/status`, {
        credentials: "include",
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
    const id = window.setInterval(() => {
      if (polling) void refresh();
    }, 2000);
    return () => window.clearInterval(id);
  }, [polling]);

  function connect() {
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
    const timer = window.setTimeout(() => {
      setBusy(false);
      setPolling(false);
    }, 90_000);
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
        credentials: "include",
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
    <div class="google-drive-section">
      <h3 style={{ margin: "0 0 6px", fontSize: "14px" }}>Google Drive · OAuth</h3>
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

  useEffect(() => {
    let cancelled = false;
    fetch(`${backendOrigin}/api/google/config`, { credentials: "include" })
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
        credentials: "include",
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

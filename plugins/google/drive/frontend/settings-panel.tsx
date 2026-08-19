// Google plugin — custom Settings panel (React, chainlit build).
//
// Why custom (not schema-driven): the Drive OAuth flow has a popup +
// status-poll dance that doesn't fit the declarative ``settings_schema``
// shape. The plugin manifest sets ``"settings_panel": "custom"`` and
// core SettingsView discovers this file via import.meta.glob.
//
// Two top-level sections:
//
//   * Accounts section — MULTI-ACCOUNT. A list of named Google accounts,
//     each with its own OAuth client credentials and its own grant.
//     Add → (optionally copy credentials from an existing account —
//     copied at creation, never referenced) → Connect → poll
//     /api/google/status → per-account Disconnect / Set default / Delete.
//     The BE persists everything in the user's settings.json; this UI
//     just drives the flow.
//
//   * No-OAuth pickup fallback — dotted-path settings
//     ``plugins.google.driveDownloadViaPickup`` +
//     ``plugins.google.pickupDownloadsDir`` for the racy
//     downloads-folder watcher path.

import { useEffect, useState } from "react";
import {
  getDotted,
  getSettings,
  saveSettings,
  subscribeSettings,
  type PublicSettings,
} from "../../../../frontend/src/lib/settings";

interface Props {
  pluginName: string;
  backendOrigin: string;
}

interface GoogleAccountStatus {
  id: string;
  label: string;
  configured: boolean;
  connected: boolean;
  needs_reauth: boolean;
  has_sheets_scope: boolean;
  account_email?: string;
  scopes?: string[];
  expires_in_s?: number;
}

interface GoogleStatus {
  default_account: string | null;
  accounts: GoogleAccountStatus[];
  configured: boolean;
  connected: boolean;
}

interface GoogleClientConfig {
  label: string;
  clientId: string;
  clientSecret: string;
}

export default function GoogleSettingsPanel({ backendOrigin }: Props) {
  const [snapshot, setSnapshot] = useState<PublicSettings>(getSettings);
  useEffect(() => subscribeSettings(setSnapshot), []);

  const pickupOn = !!getDotted(
    snapshot as unknown as Record<string, unknown>,
    "plugins.google.driveDownloadViaPickup",
  );
  const pickupDir =
    (getDotted(
      snapshot as unknown as Record<string, unknown>,
      "plugins.google.pickupDownloadsDir",
    ) as string | undefined) ?? "";

  async function patchDotted(key: string, value: unknown) {
    await saveSettings(backendOrigin, { dotted: { [key]: value } });
  }

  return (
    <div className="plugin-settings-panel google-settings">
      <GoogleAccountsSection backendOrigin={backendOrigin} />

      <hr style={{ margin: "20px 0", border: 0, borderTop: "1px solid var(--voitta-border)" }} />

      <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>No-OAuth pickup fallback</h3>
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          checked={pickupOn}
          onChange={(e) => patchDotted("plugins.google.driveDownloadViaPickup", e.currentTarget.checked)}
        />
        <span>Drive download via Downloads-folder pickup (no OAuth)</span>
      </label>
      <p className="muted">
        Off by default. Hacky workaround for when you don't want to set up
        Google OAuth: the LLM gets a <code>drive_pickup_to_python_storage</code>
        tool that opens the Drive download URL in a new tab (your existing
        Google session does the auth) and then watches the directory below for
        the resulting file. Visible to the LLM only when this is on AND no
        OAuth account is connected. Racy by design — concurrent downloads can
        be misattributed.
      </p>

      <label htmlFor="pickupDownloadsDir">Downloads folder to watch</label>
      <input
        id="pickupDownloadsDir"
        type="text"
        value={pickupDir}
        placeholder="~/Downloads"
        onChange={(e) => patchDotted("plugins.google.pickupDownloadsDir", e.currentTarget.value)}
        disabled={!pickupOn}
      />
      <p className="muted">
        Default <code>~/Downloads</code>. Tilde and environment variables are
        expanded server-side. Only used when the pickup option above is enabled.
      </p>
    </div>
  );
}

// ---- Accounts section ----------------------------------------------------

function GoogleAccountsSection({ backendOrigin }: { backendOrigin: string }) {
  const [status, setStatus] = useState<GoogleStatus | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);
  // null = closed; "" = create form; account id = edit form.
  const [formFor, setFormFor] = useState<string | null>(null);

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling]);

  function connect(accountId: string) {
    setBusyId(accountId);
    setPolling(true);
    setErr(null);
    const url = `${backendOrigin}/api/google/oauth/start?account=${encodeURIComponent(accountId)}`;
    const w = window.open(url, "_blank", "width=520,height=640");
    if (!w) {
      setBusyId(null);
      setPolling(false);
      setErr("Popup blocked. Allow popups for this page and retry.");
      return;
    }
    const timer = window.setTimeout(() => {
      setBusyId(null);
      setPolling(false);
    }, 90_000);
    const interval = window.setInterval(() => {
      if (w.closed) {
        window.clearInterval(interval);
        window.setTimeout(async () => {
          await refresh();
          setBusyId(null);
          setPolling(false);
          window.clearTimeout(timer);
        }, 800);
      }
    }, 500);
  }

  async function post(path: string, confirmMsg?: string, method = "POST") {
    if (confirmMsg && !confirm(confirmMsg)) return;
    setErr(null);
    try {
      const r = await fetch(`${backendOrigin}${path}`, {
        method,
        credentials: "include",
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(`status ${r.status}: ${detail.slice(0, 200)}`);
      }
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  const accounts = status?.accounts ?? [];
  const editingAccount = formFor
    ? accounts.find((a) => a.id === formFor) ?? null
    : null;

  return (
    <div className="google-drive-section">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h3 style={{ margin: "0 0 6px", fontSize: 14 }}>Google accounts · OAuth</h3>
        <button
          type="button"
          className="secondary-btn"
          onClick={() => setFormFor(formFor === "" ? null : "")}
          style={{ padding: "4px 10px", fontSize: 12 }}
        >
          {formFor === "" ? "Cancel" : "+ Add account"}
        </button>
      </div>

      {status === null && <p className="muted">Loading…</p>}
      {status !== null && accounts.length === 0 && formFor !== "" && (
        <p className="muted">
          No Google accounts yet. Click <b>+ Add account</b> to register one —
          paste OAuth client credentials from Google Cloud Console, then
          Connect. Drive/Sheets tools become visible to the LLM once at least
          one account is connected. Multiple accounts are supported; the LLM
          picks per call (email or label) and falls back to the default.
        </p>
      )}

      {formFor === "" && (
        <AccountForm
          backendOrigin={backendOrigin}
          mode="create"
          accounts={accounts}
          onClose={async (saved) => {
            setFormFor(null);
            if (saved) await refresh();
          }}
        />
      )}

      {accounts.map((a) => (
        <div
          key={a.id}
          style={{
            border: "1px solid var(--voitta-border)",
            borderRadius: 6,
            padding: "8px 10px",
            margin: "8px 0",
          }}
        >
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
            <span title={a.connected ? "connected" : "not connected"}>
              {a.connected ? "●" : "○"}
            </span>
            <b>{a.label}</b>
            {a.account_email && <span className="muted">{a.account_email}</span>}
            {status?.default_account === a.id && (
              <span
                style={{
                  fontSize: 11,
                  border: "1px solid var(--voitta-border)",
                  borderRadius: 4,
                  padding: "0 6px",
                }}
              >
                default
              </span>
            )}
          </div>
          <p className="muted" style={{ margin: "4px 0 6px" }}>
            {!a.configured && "No credentials yet — click Configure."}
            {a.configured && !a.connected && "Configured, not connected."}
            {a.connected && (
              <>
                Drive ✓ · Sheets {a.has_sheets_scope ? "✓" : "✗"}
                {a.needs_reauth && (
                  <b style={{ color: "var(--voitta-warn-fg)" }}>
                    {" "}
                    — missing scopes, reconnect
                  </b>
                )}
              </>
            )}
          </p>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {!a.connected && (
              <button
                type="button"
                className="save-btn"
                disabled={busyId === a.id || !a.configured}
                title={!a.configured ? "Configure credentials first" : "Sign in with Google"}
                onClick={() => connect(a.id)}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                {busyId === a.id ? "Waiting for consent…" : a.needs_reauth ? "Reconnect" : "Connect"}
              </button>
            )}
            {a.connected && a.needs_reauth && (
              <button
                type="button"
                className="save-btn"
                disabled={busyId === a.id}
                onClick={() => connect(a.id)}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                {busyId === a.id ? "Waiting for consent…" : "Reconnect"}
              </button>
            )}
            {a.connected && (
              <button
                type="button"
                className="secondary-btn"
                onClick={() =>
                  post(
                    `/api/google/accounts/${a.id}/disconnect`,
                    `Disconnect ${a.account_email || a.label}? Tools using this account will stop working.`,
                  )
                }
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                Disconnect
              </button>
            )}
            <button
              type="button"
              className="secondary-btn"
              onClick={() => setFormFor(formFor === a.id ? null : a.id)}
              style={{ padding: "4px 10px", fontSize: 12 }}
            >
              {formFor === a.id ? "Cancel" : "Configure"}
            </button>
            {status?.default_account !== a.id && (
              <button
                type="button"
                className="secondary-btn"
                onClick={() => post(`/api/google/accounts/${a.id}/default`)}
                style={{ padding: "4px 10px", fontSize: 12 }}
              >
                Set default
              </button>
            )}
            <button
              type="button"
              className="secondary-btn"
              onClick={() =>
                post(
                  `/api/google/accounts/${a.id}`,
                  `Delete account "${a.label}"${a.account_email ? ` (${a.account_email})` : ""}? ` +
                    "Its credentials and connection are removed. Saved scripts " +
                    "pinned to it will fail with a clear error until re-pinned.",
                  "DELETE",
                )
              }
              style={{ padding: "4px 10px", fontSize: 12 }}
            >
              ✕
            </button>
          </div>
          {formFor === a.id && editingAccount && (
            <AccountForm
              backendOrigin={backendOrigin}
              mode="edit"
              account={editingAccount}
              accounts={accounts}
              onClose={async (saved) => {
                setFormFor(null);
                if (saved) await refresh();
              }}
            />
          )}
        </div>
      ))}

      {err && (
        <p className="status err" style={{ marginTop: 6 }}>
          {err}
        </p>
      )}
    </div>
  );
}

// ---- Add / edit account form ----------------------------------------------

function AccountForm({
  backendOrigin,
  mode,
  account,
  accounts,
  onClose,
}: {
  backendOrigin: string;
  mode: "create" | "edit";
  account?: GoogleAccountStatus;
  accounts: GoogleAccountStatus[];
  onClose: (saved: boolean) => void | Promise<void>;
}) {
  const [label, setLabel] = useState(account?.label ?? "");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [pasteJson, setPasteJson] = useState("");
  const [parseErr, setParseErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  // Edit mode: prefill from the account's saved config.
  useEffect(() => {
    if (mode !== "edit" || !account) return;
    let cancelled = false;
    fetch(`${backendOrigin}/api/google/config?account=${encodeURIComponent(account.id)}`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`status ${r.status}`))))
      .then((c: GoogleClientConfig) => {
        if (cancelled) return;
        setLabel(c.label || account.label || "");
        setClientId(c.clientId || "");
        setClientSecret(c.clientSecret || "");
      })
      .catch(() => {
        /* non-fatal — start empty */
      });
    return () => {
      cancelled = true;
    };
  }, [backendOrigin, mode, account]);

  // Create mode: copy credentials from an existing account (one-time
  // copy of the values — accounts stay independent afterwards).
  async function copyFrom(accountId: string) {
    if (!accountId) return;
    setParseErr(null);
    try {
      const r = await fetch(
        `${backendOrigin}/api/google/config?account=${encodeURIComponent(accountId)}`,
        { credentials: "include" },
      );
      if (!r.ok) throw new Error(`status ${r.status}`);
      const c = (await r.json()) as GoogleClientConfig;
      setClientId(c.clientId || "");
      setClientSecret(c.clientSecret || "");
    } catch (e) {
      setParseErr(`Couldn't copy credentials: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

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

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.currentTarget.files?.[0];
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
    e.currentTarget.value = "";
  }

  async function onSave() {
    if (!label.trim()) {
      setSaveErr("A label is required (e.g. 'Work', 'Personal').");
      return;
    }
    if (!clientId.trim() || !clientSecret.trim()) {
      setSaveErr("Both client ID and client secret are required.");
      return;
    }
    if (
      mode === "edit" &&
      account?.connected &&
      !confirm(
        "Saving new credentials will disconnect this account's Google session. Continue?",
      )
    ) {
      return;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      const url =
        mode === "create"
          ? `${backendOrigin}/api/google/accounts`
          : `${backendOrigin}/api/google/accounts/${account!.id}/configure`;
      const r = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: label.trim(),
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

  const copySources = accounts.filter((a) => a.configured && a.id !== account?.id);

  return (
    <div className="configure-form" style={{ marginTop: 8 }}>
      <p className="muted" style={{ marginTop: 0 }}>
        Upload <code>credentials.json</code> from Google Cloud Console, paste
        the JSON, or fill the fields manually. The redirect URI registered
        in your OAuth client must match{" "}
        <code>{backendOrigin}/api/google/oauth/callback</code>. One OAuth
        client can authorize several Google accounts — use "copy credentials"
        to reuse an existing client for another account.
      </p>

      <label htmlFor="g-account-label">Label</label>
      <input
        id="g-account-label"
        type="text"
        spellCheck={false}
        autoComplete="off"
        value={label}
        onChange={(e) => setLabel(e.currentTarget.value)}
        placeholder="e.g. Work, Personal, Client X"
      />

      {mode === "create" && copySources.length > 0 && (
        <>
          <label style={{ marginTop: 10 }}>Copy credentials from</label>
          <select defaultValue="" onChange={(e) => void copyFrom(e.currentTarget.value)}>
            <option value="">— don't copy —</option>
            {copySources.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
                {a.account_email ? ` (${a.account_email})` : ""}
              </option>
            ))}
          </select>
          <p className="muted">
            One-time copy of the client ID/secret. The accounts stay
            independent — changing one later never affects the other.
          </p>
        </>
      )}

      <label style={{ marginTop: 10 }}>Upload JSON file</label>
      <input type="file" accept=".json,application/json" onChange={onFileChange} />

      <label style={{ marginTop: 10 }}>Or paste JSON</label>
      <textarea
        value={pasteJson}
        onChange={(e) => setPasteJson(e.currentTarget.value)}
        rows={4}
        spellCheck={false}
        style={{ font: "12px var(--voitta-font-mono)" }}
        placeholder='{"web": {"client_id": "...", "client_secret": "...", ...}}'
      />
      <div style={{ marginTop: 6, display: "flex", alignItems: "center", gap: 8 }}>
        <button
          type="button"
          onClick={onParseClick}
          className="secondary-btn"
          style={{ padding: "5px 12px", fontSize: 12 }}
        >
          Parse JSON
        </button>
        {parseErr && <span className="status err" style={{ fontSize: 11 }}>{parseErr}</span>}
      </div>

      <label htmlFor="g-client-id" style={{ marginTop: 12 }}>Client ID</label>
      <input
        id="g-client-id"
        type="text"
        spellCheck={false}
        autoComplete="off"
        value={clientId}
        onChange={(e) => setClientId(e.currentTarget.value)}
        placeholder="...apps.googleusercontent.com"
      />

      <label htmlFor="g-client-secret">Client secret</label>
      <input
        id="g-client-secret"
        className="secret"
        type="text"
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        value={clientSecret}
        onChange={(e) => setClientSecret(e.currentTarget.value)}
        placeholder="GOCSPX-..."
      />

      {mode === "edit" && account?.connected && (
        <p className="muted status" style={{ marginTop: 8, color: "var(--voitta-warn-fg)" }}>
          Saving changed credentials will disconnect this account's session —
          the existing tokens belong to the old client. (A label-only change
          keeps the connection.)
        </p>
      )}

      <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
        <button type="button" onClick={onSave} disabled={saving} className="save-btn">
          {saving ? "Saving…" : mode === "create" ? "Add account" : "Save"}
        </button>
        <button
          type="button"
          onClick={() => onClose(false)}
          disabled={saving}
          className="secondary-btn"
        >
          Cancel
        </button>
        {saveErr && <span className="status err" style={{ fontSize: 11 }}>{saveErr}</span>}
      </div>
    </div>
  );
}

// Login screen shown by the widget when the backend reports
// ``localhost_mode=false, authenticated=false``. The user types the
// shared API key (placeholder for an eventual Google sign-in flow).
// On success the backend sets a session cookie; the parent re-checks
// /api/auth/status and unmounts this view.

import { useState } from "preact/hooks";

import { loginWithApiKey } from "../lib/auth";

interface Props {
  backendOrigin: string;
  onAuthenticated: () => void;
}

export function LoginView({ backendOrigin, onAuthenticated }: Props) {
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const result = await loginWithApiKey(backendOrigin, apiKey.trim());
    setBusy(false);
    if (result.ok) {
      setApiKey("");
      onAuthenticated();
    } else {
      setError(result.error || "Login failed.");
    }
  }

  return (
    <section
      class="login-pane"
      role="form"
      aria-label="Voitta login"
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "stretch",
        justifyContent: "center",
        padding: "24px",
        gap: "12px",
      }}
    >
      <h2 style={{ margin: 0, fontSize: "16px", fontWeight: 600 }}>
        Sign in to continue
      </h2>
      <p class="muted" style={{ margin: 0 }}>
        This Voitta backend requires an API key. Enter it once; the
        session cookie persists across reloads.
      </p>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <label htmlFor="voitta-api-key">API key</label>
        <input
          id="voitta-api-key"
          type="password"
          value={apiKey}
          autoComplete="off"
          autoFocus
          disabled={busy}
          onInput={(e) => setApiKey((e.currentTarget as HTMLInputElement).value)}
          placeholder="enter key"
          style={{
            padding: "8px 10px",
            border: "1px solid var(--voitta-border)",
            borderRadius: "4px",
            background: "var(--voitta-surface)",
            color: "var(--voitta-text)",
            font: "inherit",
          }}
        />
        <button
          type="submit"
          disabled={busy || !apiKey.trim()}
          class="save-btn"
          style={{ marginTop: "4px" }}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error && (
        <div class="status err" role="alert" aria-live="polite">
          {error}
        </div>
      )}
      <p class="scope" style={{ marginTop: "auto" }}>
        Run the backend with <code>--localhost</code> to skip this screen
        on a single-user loopback deployment.
      </p>
    </section>
  );
}

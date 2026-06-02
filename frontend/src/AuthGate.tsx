// Server-mode login gate. Renders a "Sign in with Google" panel until the
// backend reports an authenticated session, then renders its children. On
// desktop/dev the backend reports { enabled: false } and children render
// immediately — no login step.

import { useCallback, useEffect, useState } from "react";
import { AuthContext, checkAuth, logout, startGoogleLogin, type AuthState } from "./lib/auth";

interface Props {
  backendOrigin: string;
  children: React.ReactNode;
}

export default function AuthGate({ backendOrigin, children }: Props) {
  const [state, setState] = useState<AuthState | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setState(await checkAuth(backendOrigin));
  }, [backendOrigin]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onLogin = useCallback(async () => {
    setBusy(true);
    try {
      await startGoogleLogin(backendOrigin);
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [backendOrigin, refresh]);

  // Logout clears the session then re-probes; refresh() flips state to
  // unauthenticated, which re-renders this gate back to the login screen.
  const onLogout = useCallback(async () => {
    await logout(backendOrigin);
    await refresh();
  }, [backendOrigin, refresh]);

  // Still probing.
  if (state === null) return null;

  // Guard off (desktop/dev) or already signed in → show the app, exposing
  // identity + logout to the tree (email is null on desktop).
  if (!state.enabled || state.authenticated) {
    return (
      <AuthContext.Provider
        value={{ enabled: state.enabled, email: state.email, logout: onLogout }}
      >
        {children}
      </AuthContext.Provider>
    );
  }

  return (
    <div className="root" data-theme="dark">
      {/* Docked panel mirroring the drawer's geometry — confined to the
          widget's pane, NOT a full-page overlay. The shadow host is
          pointer-events:none; this panel re-enables them for itself. */}
      <div
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "min(420px, 92vw)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "var(--voitta-bg)",
          color: "var(--voitta-text)",
          borderLeft: "1px solid var(--voitta-border)",
          boxShadow: "var(--voitta-shadow)",
          font: "14px system-ui, sans-serif",
          pointerEvents: "auto",
          zIndex: 2147483647,
        }}
      >
        <div style={{ textAlign: "center", maxWidth: 280, padding: "0 24px" }}>
          <div style={{ fontSize: 22, fontWeight: 700, marginBottom: 6 }}>Voitta</div>
          <div style={{ opacity: 0.7, marginBottom: 24 }}>
            Sign in with Google to continue.
          </div>
          <button
            type="button"
            onClick={onLogin}
            disabled={busy}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 18px",
              borderRadius: 8,
              border: "1px solid #2a3548",
              background: busy ? "#1f2733" : "#f1f5f9",
              color: busy ? "#a0a3ad" : "#0f172a",
              fontWeight: 600,
              cursor: busy ? "default" : "pointer",
            }}
          >
            <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
              <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.2 0 24 0 14.6 0 6.4 5.4 2.5 13.2l7.8 6.1C12.2 13.3 17.6 9.5 24 9.5z" />
              <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.2-.4-4.7H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.4 5.7c4.3-4 6.8-9.9 6.8-17.2z" />
              <path fill="#FBBC05" d="M10.3 19.3l-7.8-6.1C.9 16.5 0 20.1 0 24s.9 7.5 2.5 10.8l7.8-6.1c-.5-1.5-.8-3-.8-4.7s.3-3.2.8-4.7z" />
              <path fill="#34A853" d="M24 48c6.2 0 11.5-2 15.3-5.5l-7.4-5.7c-2 1.4-4.7 2.3-7.9 2.3-6.4 0-11.8-3.8-13.7-9.3l-7.8 6.1C6.4 42.6 14.6 48 24 48z" />
            </svg>
            {busy ? "Signing in…" : "Sign in with Google"}
          </button>
        </div>
      </div>
    </div>
  );
}

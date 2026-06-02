// Login-guard client. Mirrors the backend /api/auth/* routes.
//
// The guard is server-mode only; on desktop/dev the backend reports
// { enabled: false } and the app renders as usual without any login step.

import { createContext, useContext } from "react";

export interface AuthState {
  enabled: boolean;
  authenticated: boolean;
  email: string | null;
}

const OPEN: AuthState = { enabled: false, authenticated: false, email: null };

// Identity + logout exposed to the app tree by AuthGate. On desktop/dev
// enabled=false and email=null, so consumers simply hide the user UI.
export interface AuthInfo {
  enabled: boolean;
  email: string | null;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthInfo>({
  enabled: false,
  email: null,
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);

export async function checkAuth(backendOrigin: string): Promise<AuthState> {
  try {
    const res = await fetch(`${backendOrigin}/api/auth/me`, {
      credentials: "include",
    });
    if (res.ok) return (await res.json()) as AuthState;
  } catch (err) {
    console.warn("[voitta] auth check failed", err);
  }
  // If the probe itself fails, fail open to the pre-auth behaviour rather
  // than hard-locking the UI — guarded data endpoints still 401 on their own.
  return OPEN;
}

// Clear the session cookie (server-side). The next sign-in re-prompts for
// account selection (build_authorize_url uses prompt=select_account), so this
// is a clean app logout without nuking the user's global Google session.
export async function logout(backendOrigin: string): Promise<void> {
  try {
    await fetch(`${backendOrigin}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  } catch (err) {
    console.warn("[voitta] logout failed", err);
  }
}

// Open the Google sign-in flow in a popup and resolve once it completes
// (the callback page posts "voitta-auth" to the opener, then self-closes).
export function startGoogleLogin(backendOrigin: string): Promise<void> {
  return new Promise((resolve) => {
    const popup = window.open(
      `${backendOrigin}/api/auth/google/start`,
      "voitta-login",
      "width=480,height=680",
    );

    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onMsg);
      clearInterval(poll);
      resolve();
    };
    const onMsg = (e: MessageEvent) => {
      if (e.data === "voitta-auth") finish();
    };
    window.addEventListener("message", onMsg);
    // Fallback: detect the popup closing even if postMessage was blocked.
    const poll = window.setInterval(() => {
      if (!popup || popup.closed) finish();
    }, 700);
  });
}

// Login-guard client. Mirrors the backend /api/auth/* routes.
//
// The guard is server-mode only; on desktop/dev the backend reports
// { enabled: false } and the app renders as usual without any login step.

export interface AuthState {
  enabled: boolean;
  authenticated: boolean;
  email: string | null;
}

const OPEN: AuthState = { enabled: false, authenticated: false, email: null };

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

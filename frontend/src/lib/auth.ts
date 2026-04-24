// Auth gate for non-localhost mode. The backend at /api/auth/status
// reports two booleans: ``localhost_mode`` (auth disabled) and
// ``authenticated`` (cookie matches). The widget mounts the chat when
// either is true; otherwise it renders the LoginView.

export interface AuthStatus {
  localhostMode: boolean;
  authenticated: boolean;
}

export async function fetchAuthStatus(backendOrigin: string): Promise<AuthStatus> {
  const url = backendOrigin.replace(/\/$/, "") + "/api/auth/status";
  const res = await fetch(url, {
    method: "GET",
    credentials: "include",
  });
  if (!res.ok) {
    // Treat any unexpected response as "needs auth" — fail closed.
    return { localhostMode: false, authenticated: false };
  }
  const body = (await res.json()) as {
    localhost_mode?: boolean;
    authenticated?: boolean;
  };
  return {
    localhostMode: !!body.localhost_mode,
    authenticated: !!body.authenticated,
  };
}

export async function loginWithApiKey(
  backendOrigin: string,
  apiKey: string,
): Promise<{ ok: boolean; error?: string }> {
  const url = backendOrigin.replace(/\/$/, "") + "/api/auth/login";
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
      credentials: "include",
    });
  } catch (err) {
    return { ok: false, error: `network error: ${(err as Error).message}` };
  }
  if (res.status === 401) {
    return { ok: false, error: "Invalid API key." };
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    return { ok: false, error: `HTTP ${res.status}: ${text || res.statusText}` };
  }
  return { ok: true };
}

export async function logout(backendOrigin: string): Promise<void> {
  const url = backendOrigin.replace(/\/$/, "") + "/api/auth/logout";
  await fetch(url, {
    method: "POST",
    credentials: "include",
  }).catch(() => {});
}

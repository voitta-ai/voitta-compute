// Top-level widget shell. Decides between ChatPane (normal) and
// LoginView (LAN deployments where the user hasn't entered the API
// key yet) by polling /api/auth/status on mount.
//
// While the status probe is in flight we render nothing — the chat
// drawer would briefly flash open then close on first paint, which
// looks broken. The probe is one HTTP round-trip to a cheap route,
// so the blank window is sub-100ms in practice.

import { useEffect, useState } from "preact/hooks";

import { ChatPane } from "./components/ChatPane";
import { LoginView } from "./components/LoginView";
import { fetchAuthStatus, type AuthStatus } from "./lib/auth";
import { startBridge } from "./lib/bridge";
import { log } from "./lib/logger";

interface Props {
  backendOrigin: string;
}

type View = "loading" | "login" | "chat";

export function App({ backendOrigin }: Props) {
  const [view, setView] = useState<View>("loading");
  const [bridgeStarted, setBridgeStarted] = useState(false);

  async function recheck() {
    let status: AuthStatus;
    try {
      status = await fetchAuthStatus(backendOrigin);
    } catch (err) {
      log.warn("auth", "fetchAuthStatus failed", { err: String(err) });
      // Fail closed — show the login screen so the user has a recovery
      // path. They can retry the key entry; if the backend is truly
      // down, they'll see a network error from /api/auth/login too.
      setView("login");
      return;
    }
    setView(status.localhostMode || status.authenticated ? "chat" : "login");
  }

  useEffect(() => {
    void recheck();
  }, [backendOrigin]);

  // Start the SSE bridge only AFTER we know we're allowed. In auth
  // mode the bridge's EventSource needs a valid cookie, which the
  // browser can't have until /api/auth/login has set it.
  useEffect(() => {
    if (view === "chat" && !bridgeStarted) {
      startBridge(backendOrigin);
      setBridgeStarted(true);
    }
  }, [view, bridgeStarted, backendOrigin]);

  if (view === "loading") {
    return null;
  }
  if (view === "login") {
    return (
      <LoginView
        backendOrigin={backendOrigin}
        onAuthenticated={() => void recheck()}
      />
    );
  }
  return <ChatPane backendOrigin={backendOrigin} />;
}

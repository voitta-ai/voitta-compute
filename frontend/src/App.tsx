// Root component. Boots the settings cache, then renders the
// Chainlit context provider + Recoil root + the styled drawer chrome.

import { ChainlitAPI, ChainlitContext } from "@chainlit/react-client";
import { RecoilRoot } from "recoil";
import { useEffect, useMemo, useState } from "react";
import Drawer from "./Drawer";
import AuthGate from "./AuthGate";
import { bootstrapSettings } from "./lib/settings";

interface Props {
  backendOrigin: string;
}

// The authenticated app. Bootstraps the settings cache (a guarded endpoint
// in server mode), then renders the Chainlit context + drawer. Mounted only
// once AuthGate is satisfied, so /api/settings is never called pre-auth.
function AppInner({ backendOrigin }: Props) {
  const api = useMemo(
    () => new ChainlitAPI(`${backendOrigin}/chainlit`, "webapp"),
    [backendOrigin],
  );
  const [ready, setReady] = useState(false);

  useEffect(() => {
    bootstrapSettings(backendOrigin).finally(() => setReady(true));
  }, [backendOrigin]);

  if (!ready) return null;

  return (
    <ChainlitContext.Provider value={api}>
      <RecoilRoot>
        <Drawer backendOrigin={backendOrigin} />
      </RecoilRoot>
    </ChainlitContext.Provider>
  );
}

export default function App({ backendOrigin }: Props) {
  return (
    <AuthGate backendOrigin={backendOrigin}>
      <AppInner backendOrigin={backendOrigin} />
    </AuthGate>
  );
}

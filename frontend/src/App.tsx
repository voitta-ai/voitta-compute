// Root component. Boots the settings cache, then renders the
// Chainlit context provider + Recoil root + the styled drawer chrome.

import { ChainlitAPI, ChainlitContext } from "@chainlit/react-client";
import { RecoilRoot } from "recoil";
import { useEffect, useMemo, useState } from "react";
import Drawer from "./Drawer";
import { bootstrapSettings } from "./lib/settings";

interface Props {
  backendOrigin: string;
}

export default function App({ backendOrigin }: Props) {
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

import { useEffect, useState } from "react";
import { getSettings, subscribeSettings, type PublicSettings } from "./settings";

export function useSettings(): PublicSettings {
  const [s, setS] = useState<PublicSettings>(() => getSettings());
  useEffect(() => subscribeSettings(setS), []);
  return s;
}

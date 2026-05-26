// HTML report iframe pane.
//
// Same wire shape as PanelPane (server pre-renders HTML, FE just
// mounts an iframe at the URL) — but the iframe loads a fully
// pre-rasterised Jinja-rendered document from /api/html-report.
//
// No Bokeh layout engine inside, so the iframe geometry only matters
// for the screenshot path (where the existing primitive resizes it
// to a desktop-class width before capture).

import { useMemo } from "react";
import { useSetRecoilState } from "recoil";
import { reportLoadingState } from "../state";
import type { HtmlPayload } from "../types";

interface Props {
  backendOrigin: string;
  reportName: string;
  renderId: string;
  payload: HtmlPayload;
}

export default function HtmlPane({ backendOrigin, payload }: Props) {
  const setLoading = useSetRecoilState(reportLoadingState);
  const src = useMemo(() => {
    try {
      return new URL(payload.url, backendOrigin).toString();
    } catch {
      return backendOrigin + payload.url;
    }
  }, [backendOrigin, payload.url]);

  return (
    <iframe
      src={src}
      title={payload.title || payload.url}
      className="report-panel-iframe"
      style={{
        width: "100%",
        height: "100%",
        border: 0,
        display: "block",
        background: "var(--voitta-bg, transparent)",
      }}
      onLoad={() => setLoading(false)}
    />
  );
}

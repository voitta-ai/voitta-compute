// Single report kind: HTML. Always mounts HtmlPane.

import type { ActiveReport } from "./types";
import HtmlPane from "./kinds/HtmlPane";

interface Props {
  backendOrigin: string;
  report: ActiveReport;
}

export default function ReportRenderer({ backendOrigin, report }: Props) {
  return (
    <HtmlPane
      backendOrigin={backendOrigin}
      reportName={report.name}
      renderId={report.render_id}
      payload={report.payload}
    />
  );
}

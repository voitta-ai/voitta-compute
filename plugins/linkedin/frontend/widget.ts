// LinkedIn plugin — frontend primitives. Side-effect import; voitta
// core globs every plugin's frontend/widget.ts and bundles them into
// widget.js.
//
// One primitive:
//   • linkedin_inspect_page — what page is the user on?

import { registerPrimitive } from "../../../frontend/src/lib/bridge";


function _classifyPage(): string {
  const p = location.pathname;
  if (p === "/" || p === "/feed" || p.startsWith("/feed/")) return "feed";
  if (p.startsWith("/in/")) return "profile";
  if (p.startsWith("/company/")) return "company";
  if (p.startsWith("/jobs/view/")) return "job";
  if (p.startsWith("/jobs/")) return "jobs";
  if (p.startsWith("/messaging/")) return "messaging";
  if (p.startsWith("/mynetwork/")) return "mynetwork";
  if (p.startsWith("/notifications/")) return "notifications";
  if (p.startsWith("/search/")) return "search";
  return "other";
}


registerPrimitive("linkedin_inspect_page", async () => {
  const params = Object.fromEntries(new URLSearchParams(location.search));
  return {
    url: location.href,
    pathname: location.pathname,
    title: document.title,
    page_type: _classifyPage(),
    profile_id: location.pathname.match(/^\/in\/([^/]+)/)?.[1] || null,
    company_slug: location.pathname.match(/^\/company\/([^/]+)/)?.[1] || null,
    job_id: location.pathname.match(/^\/jobs\/view\/(\d+)/)?.[1] || null,
    params,
  };
});

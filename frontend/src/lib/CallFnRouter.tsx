import { callFnState, useChatData, useChatInteract } from "@chainlit/react-client";
import { useEffect } from "react";
import { useRecoilState, useSetRecoilState } from "recoil";
import { activeTabState, reportCollapsedState, reportsState } from "../report/state";
import type { ShowHtmlReportArgs } from "../report/types";
import { primitives } from "./primitives";
import { requestToken } from "./tokenPrompt";

export default function CallFnRouter() {
  const [callFn, setCallFn] = useRecoilState(callFnState);
  const [reports, setReports] = useRecoilState(reportsState);
  const setActiveTab = useSetRecoilState(activeTabState);
  const setCollapsed = useSetRecoilState(reportCollapsedState);
  const { sendMessage } = useChatInteract();
  const { loading } = useChatData();

  useEffect(() => {
    if (!callFn) return;
    const { name, args, callback } = callFn;

    (async () => {
      let result: unknown;
      try {
        if (name === "show_html_report") {
          const a = args as unknown as ShowHtmlReportArgs;
          const entry = {
            name: a.name,
            title: a.title ?? null,
            render_id: a.render_id,
            payload: { kind: "html" as const, url: a.url, title: a.title ?? null },
          };
          setReports((prev) => {
            // Same script name → replace in-place (re-run of existing tab).
            // Same render_id → also replace. Otherwise append.
            const byName = prev.findIndex((r) => r.name === a.name);
            const byId   = prev.findIndex((r) => r.render_id === a.render_id);
            const idx = byName >= 0 ? byName : byId;
            return idx >= 0
              ? prev.map((r, i) => (i === idx ? entry : r))
              : [...prev, entry];
          });
          setActiveTab(a.render_id);
          setCollapsed(false);
          result = { ok: true };
        } else if (name === "close_report") {
          const a = args as { name?: string };
          setReports((prev) =>
            a.name ? prev.filter((r) => r.name !== a.name) : []
          );
          result = { ok: true };
        } else if (name === "submit_user_text") {
          // Inject a user message as if typed in the Composer — same
          // sendMessage path, so the BE sees a normal user_message.
          // Used by the voice assistant (and the MCP debug backdoor).
          const text = String((args as { text?: unknown })?.text ?? "").trim();
          if (!text) {
            result = { ok: false, error: "empty_text" };
          } else if (loading) {
            result = { ok: false, error: "busy", message: "a turn is already running" };
          } else {
            sendMessage({ output: text, name: "user", type: "user_message" }, []);
            result = { ok: true };
          }
        } else if (name === "prompt_claude_token") {
          // Open the masked-input modal and return the token over this ACK.
          // The value never goes through sendMessage, so it never persists to
          // a chat step or the conversation DB.
          const instructions = String(
            (args as { instructions?: unknown })?.instructions ?? "",
          );
          result = await requestToken(instructions);
        } else {
          const impl = primitives[name];
          result = impl
            ? await impl(args ?? {})
            : { error: `unknown browser tool ${name}` };
        }
      } catch (err) {
        result = { error: String(err) };
      }
      try {
        callback(result as Record<string, unknown>);
      } finally {
        setCallFn(undefined);
      }
    })();
  }, [callFn, setCallFn, setReports, setActiveTab, setCollapsed, sendMessage, loading]);

  return null;
}

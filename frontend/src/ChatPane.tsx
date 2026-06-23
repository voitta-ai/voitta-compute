// Chat surface: socket connect, message list, composer with image
// attachments. On send, each attachment is uploaded to Chainlit's
// file endpoint and then referenced from the user message — the BE
// pulls them off ``cl.Message.elements`` and attaches them to the
// model call.

import {
  useChatData,
  useChatInteract,
  useChatMessages,
} from "@chainlit/react-client";
import { useEffect, useState } from "react";
import MessageList from "./chat/MessageList";
import Composer from "./chat/Composer";
import TokenPromptModal from "./TokenPromptModal";
import type { ImageAttachment } from "./lib/image-attach";
import { encodeFiles } from "./lib/attachments";
import { useAuthConnect } from "./lib/useAuthConnect";

interface Props {
  backendOrigin: string;
  hasApiKey: boolean;
  threadId?: string | null;
}

export default function ChatPane({ backendOrigin, hasApiKey, threadId }: Props) {
  // useAuthConnect is Chainlit's useChatSession, which takes no args — the
  // connection target is configured on the ChainlitContext provider, not here.
  // (backendOrigin is still used below for socket transport selection.)
  const { connect, disconnect, session } = useAuthConnect();
  const { messages } = useChatMessages();
  const { loading, elements } = useChatData();
  const { sendMessage, stopTask, uploadFile, windowMessage } = useChatInteract();
  const [attachments, setAttachments] = useState<ImageAttachment[]>([]);

  // Drawer sets threadIdToResumeState (and clears messages) BEFORE changing the key that
  // remounts this component. So by the time this component mounts, the Recoil atom already
  // holds the correct value and the `connect` closure captures it.
  //
  // Effect 1 (mount): just disconnect on cleanup.
  // Effect 2 (connect changes): always call connect(). Fires on mount (initial value) and
  // whenever the connect closure rebuilds (which won't happen here since the atom was set
  // before mount and doesn't change again).
  useEffect(() => {
    return () => { disconnect(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // On the hardened-site bridge, all backend traffic is tunnelled through
    // the popup. Force socket.io to the WebSocket transport only — its
    // XHR-polling fallback isn't covered by the WebSocket shim (and would hit
    // the page CSP's connect-src wall). On ordinary pages, leave the default.
    const bridge = (window as unknown as { __voittaBridge?: boolean }).__voittaBridge;
    connect(bridge ? { userEnv: {}, transports: ["websocket"] } : { userEnv: {} });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect]);

  // Tell the backend which host/page/title the bookmarklet sits on — the
  // active-sessions window ("tasks voitta") renders these per row. Title
  // can change after load (SPAs), so observe <title> and re-post.
  useEffect(() => {
    const socket = session?.socket;
    if (!socket) return;
    const post = () => {
      windowMessage(`host:${location.host}`);
      windowMessage(`url:${location.href}`);
      windowMessage(`title:${document.title}`);
    };
    post();
    socket.on("connect", post);

    const titleEl = document.querySelector("title");
    let obs: MutationObserver | null = null;
    if (titleEl) {
      obs = new MutationObserver(() => {
        windowMessage(`title:${document.title}`);
        windowMessage(`url:${location.href}`);
      });
      obs.observe(titleEl, { childList: true });
    }
    return () => {
      socket.off("connect", post);
      obs?.disconnect();
    };
  }, [session, windowMessage]);

  // Focus beacon: mark this session as the active one whenever the host
  // tab gains focus (last-focused-wins on the backend — no blur message).
  // The voice assistant routes utterances to the last-focused session.
  useEffect(() => {
    const socket = session?.socket;
    if (!socket) return;
    const beacon = () => {
      if (document.visibilityState === "visible" && document.hasFocus()) {
        windowMessage("focus:1");
      }
    };
    window.addEventListener("focus", beacon);
    document.addEventListener("visibilitychange", beacon);
    socket.on("connect", beacon);
    beacon();
    return () => {
      window.removeEventListener("focus", beacon);
      document.removeEventListener("visibilitychange", beacon);
      socket.off("connect", beacon);
    };
  }, [session, windowMessage]);

  // ``loading`` is true between ``task_start`` and ``task_end`` socket
  // events — covers the whole turn, including the gap between tool
  // dispatches when no text is streaming. ``streaming`` on the last
  // message only catches the text-output phase, so it leaves us with
  // a greyed-out send button during tool execution. Combine both for
  // safety.
  const busy = loading || messages.some((m) => m.streaming);

  async function onAttach(files: File[]) {
    const encoded = await encodeFiles(files);
    if (encoded.length) setAttachments((prev) => [...prev, ...encoded]);
  }

  function onRemoveAttachment(i: number) {
    setAttachments((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function onSend(text: string) {
    const pending = attachments;
    setAttachments([]);

    // Upload attachments first so Chainlit has File ids before the
    // user message lands. Each upload returns ``{id}``; we batch into
    // a fileReferences list and pass to ``sendMessage``.
    const refs: { id: string }[] = [];
    for (const att of pending) {
      try {
        const file = await imageAttachmentToFile(att);
        const { promise } = uploadFile(file, () => undefined);
        const { id } = await promise;
        refs.push({ id });
      } catch (err) {
        console.warn("[voitta] upload failed", err);
      }
    }

    sendMessage(
      { output: text, name: "user", type: "user_message" },
      refs,
    );
  }

  return (
    <div className="chat-pane">
      <MessageList
        steps={messages}
        elements={elements}
        backendOrigin={backendOrigin}
        emptyHint={
          hasApiKey
            ? "Say hello to get started."
            : "Open ⚙ Settings and add an API key."
        }
      />
      <TokenPromptModal />
      <Composer
        busy={busy}
        attachments={attachments}
        onAttach={onAttach}
        onRemoveAttachment={onRemoveAttachment}
        onSend={onSend}
        onStop={() => stopTask()}
      />
    </div>
  );
}

// Round-trip an encoded attachment back to a `File` so we can hand it
// to Chainlit's `uploadFile`. We already paid the encode cost in
// `encodeFiles`; the base64 → Blob conversion is cheap.
async function imageAttachmentToFile(att: ImageAttachment): Promise<File> {
  const bin = atob(att.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const ext = att.mime.split("/")[1] || "bin";
  return new File([bytes], `paste-${Date.now()}.${ext}`, { type: att.mime });
}

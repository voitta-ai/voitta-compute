// One row in the chat: user/assistant message bubble or tool step.
// Class names match styles/components/messages.css.

import { type IMessageElement, type IStep } from "@chainlit/react-client";
import Markdown from "./Markdown";

interface Props {
  step: IStep;
  elements: IMessageElement[];
  backendOrigin: string;
}

export default function StepView({ step, elements, backendOrigin }: Props) {
  if (step.type === "user_message") {
    const images = elements.filter((e) => e.type === "image");
    return (
      <div className="msg user">
        {images.length > 0 && (
          <div className="msg-attachments">
            {images.map((el) => (
              <img
                key={el.id}
                className="msg-attachment"
                src={resolveUrl(el.url, backendOrigin)}
                alt={el.name || ""}
              />
            ))}
          </div>
        )}
        {step.output ?? null}
      </div>
    );
  }
  if (step.type === "assistant_message") {
    const inline = elements.filter((e) => !("display" in e) || e.display === "inline");
    return (
      <div className={`turn assistant${step.streaming ? " streaming" : ""}`}>
        <div className="msg assistant">
          {step.output ? <Markdown text={step.output} /> : null}
          {inline.map((el) => (
            <ElementView key={el.id} element={el} backendOrigin={backendOrigin} />
          ))}
        </div>
      </div>
    );
  }
  if (step.type === "tool") {
    // step.streaming is unreliable for cl.Step — use output presence instead.
    // Output is only written by the backend when the tool finishes.
    const running = !step.output && !step.isError;
    const status: "running" | "ok" | "error" = running
      ? "running"
      : step.isError
        ? "error"
        : "ok";
    const inputChars = step.input ? String(step.input).length : 0;
    const images = elements.filter((e) => e.type === "image");

    if (running) {
      return (
        <div className={`tool-line status-running`}>
          <span className="tool-spinner" />
          <span className="name">{step.name}</span>
          {inputChars > 0 && (
            <span className="extra">{inputChars.toLocaleString()} chars</span>
          )}
        </div>
      );
    }
    return (
      <details className={`tool-line status-${status}`}>
        <summary>
          <span className="sym">{status === "ok" ? "✓" : "✗"}</span>
          <span className="name">{step.name}</span>
        </summary>
        <div className="tool-body">
          {step.input ? <pre>input: {String(step.input)}</pre> : null}
          {step.output ? <Markdown text={String(step.output)} /> : null}
          {images.map((el) => (
            <ElementView key={el.id} element={el} backendOrigin={backendOrigin} />
          ))}
        </div>
      </details>
    );
  }
  return null;
}

function ElementView({
  element,
  backendOrigin,
}: {
  element: IMessageElement;
  backendOrigin: string;
}) {
  if (element.type === "image" && element.url) {
    return (
      <figure className="rich-image">
        <img src={resolveUrl(element.url, backendOrigin)} alt={element.name || ""} />
        {element.name ? <figcaption>{element.name}</figcaption> : null}
      </figure>
    );
  }
  if (element.type === "text") {
    // Inline text elements are rendered as code/snippet blocks by Chainlit
    // convention. Markdown gives us code-fence styling for free if the
    // server wraps it; otherwise fall back to <pre>.
    return null;
  }
  return null;
}

function resolveUrl(url: string | undefined, backendOrigin: string): string {
  if (!url) return "";
  if (/^(https?:|data:|blob:)/i.test(url)) return url;
  const base = backendOrigin.replace(/\/$/, "");
  return base + (url.startsWith("/") ? url : "/" + url);
}

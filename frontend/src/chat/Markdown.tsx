import { useEffect, useRef } from "react";
import { hydrateMermaid, renderMarkdown } from "../lib/markdown";

export default function Markdown({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);

  // First paint is sync via dangerouslySetInnerHTML — mermaid blocks
  // appear as inert placeholders. The effect then asynchronously
  // replaces each placeholder with rendered SVG. Re-runs on every
  // text change (streaming deltas); the cache in markdown.ts makes
  // repeat renders of the same finished block essentially free.
  useEffect(() => {
    void hydrateMermaid(ref.current);
  }, [text]);

  return (
    <div
      ref={ref}
      className="markdown"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}

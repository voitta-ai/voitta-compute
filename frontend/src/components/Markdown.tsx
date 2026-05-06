import { useEffect, useRef } from "preact/hooks";
import { hydrateMermaid, renderMarkdown } from "../lib/markdown";

export function Markdown({ text }: { text: string }) {
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
      class="markdown"
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}

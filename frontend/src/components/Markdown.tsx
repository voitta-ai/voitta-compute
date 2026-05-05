import { renderMarkdown } from "../lib/markdown";

export function Markdown({ text }: { text: string }) {
  return (
    <div
      class="markdown"
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}

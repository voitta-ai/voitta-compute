import { describe, expect, it } from "vitest";

import { decodeEntities, reportLabel } from "../labels";

describe("decodeEntities", () => {
  it("decodes the escape that actually shipped to the tab bar", () => {
    // Observed live: a tab reading "3 · Supply &amp; co…"
    expect(decodeEntities("3 · Supply &amp; Cost")).toBe("3 · Supply & Cost");
  });

  it("decodes the rest of the common named set", () => {
    expect(decodeEntities("&lt;a&gt; &quot;x&quot; &apos;y&apos;")).toBe(`<a> "x" 'y'`);
    expect(decodeEntities("a&nbsp;b")).toBe("a b");
    expect(decodeEntities("Q3&ndash;Q4 &hellip;")).toBe("Q3–Q4 …");
  });

  it("decodes numeric and hex references", () => {
    expect(decodeEntities("&#38; &#x26; &#183;")).toBe("& & ·");
  });

  it("leaves text without entities untouched", () => {
    const plain = "4 · Customer 360";
    expect(decodeEntities(plain)).toBe(plain);
    expect(decodeEntities("")).toBe("");
  });

  it("leaves unknown or malformed entities exactly as written", () => {
    expect(decodeEntities("AT&T")).toBe("AT&T");
    expect(decodeEntities("&notareal; &amp")).toBe("&notareal; &amp");
    expect(decodeEntities("100% &copy")).toBe("100% &copy");
  });

  it("decodes one level only, so a double escape keeps its meaning", () => {
    // The author wanted the literal text "&lt;", not a "<".
    expect(decodeEntities("&amp;lt;")).toBe("&lt;");
  });

  it("degrades to raw text on out-of-range or surrogate code points", () => {
    expect(decodeEntities("&#1114112;")).toBe("&#1114112;"); // > 0x10FFFF
    expect(decodeEntities("&#xD800;")).toBe("&#xD800;"); // lone surrogate
    expect(decodeEntities("&#0;")).toBe("&#0;");
  });

  it("never interprets the title as markup", () => {
    const hostile = '&lt;img src=x onerror="boom"&gt;';
    // Decoded to visible text, and still just a string — no element is built.
    expect(decodeEntities(hostile)).toBe('<img src=x onerror="boom">');
  });
});

describe("reportLabel", () => {
  it("prefers the decoded title", () => {
    expect(reportLabel({ title: "Supply &amp; Cost", name: "supply" })).toBe("Supply & Cost");
  });

  it("falls back to the slug when the title is missing or empty", () => {
    expect(reportLabel({ title: null, name: "supply_costs" })).toBe("supply_costs");
    expect(reportLabel({ title: "", name: "supply_costs" })).toBe("supply_costs");
    expect(reportLabel({ name: "supply_costs" })).toBe("supply_costs");
  });
});

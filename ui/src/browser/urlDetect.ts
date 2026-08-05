/** Detect http(s) URLs in plain text (dev-server style lines). */

export const URL_RE =
  /https?:\/\/[^\s<>"'`)\]}>，。；！？]+/gi;

export function normalizeDetectedUrl(raw: string): string {
  let u = (raw || "").trim();
  // Trim common trailing punctuation from logs / markdown.
  u = u.replace(/[),.;:!?，。；！？]+$/g, "");
  return u;
}

export function isHttpUrl(value: string): boolean {
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export type TextSegment = { type: "text"; value: string } | { type: "url"; value: string };

export function splitTextWithUrls(text: string): TextSegment[] {
  const src = text || "";
  if (!src) return [];
  const out: TextSegment[] = [];
  const re = new RegExp(URL_RE.source, "gi");
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src))) {
    const start = m.index;
    if (start > last) out.push({ type: "text", value: src.slice(last, start) });
    const url = normalizeDetectedUrl(m[0]);
    out.push({ type: "url", value: url });
    last = start + m[0].length;
    // If we trimmed trailing chars from the match, keep them as text.
    const trimmed = m[0].length - url.length;
    if (trimmed > 0) {
      out.push({ type: "text", value: m[0].slice(url.length) });
    }
  }
  if (last < src.length) out.push({ type: "text", value: src.slice(last) });
  return out.length ? out : [{ type: "text", value: src }];
}

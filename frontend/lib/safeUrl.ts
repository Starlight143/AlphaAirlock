// Shared URL sanitization for any UI that renders user/ingest-supplied URLs
// as anchors. Returns the URL if it's an allowlisted scheme (http(s) or
// mailto), otherwise undefined so call sites can degrade to plain text.

export function safeExternalUrl(u: string | null | undefined): string | undefined {
  if (!u) return undefined;
  const trimmed = String(u).trim();
  if (!trimmed) return undefined;
  if (!/^https?:\/\//i.test(trimmed) && !/^mailto:/i.test(trimmed)) return undefined;
  return trimmed;
}

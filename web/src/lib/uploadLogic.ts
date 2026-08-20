/** Pure logic behind `ConfigForm`'s upload progress line — pulled out so it
 *  can be unit-tested without mounting Svelte. */

const UNITS = ["B", "KB", "MB", "GB"];

/** A byte count as a short, human string — "383 MB", "512 B". One decimal
 *  place from KB up; a byte count itself is never shown as a fraction. */
function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${Math.round(value)} ${UNITS[unit]}` : `${value.toFixed(1)} ${UNITS[unit]}`;
}

/** The percentage complete, or `null` when the browser has no total to
 *  measure against (`lengthComputable` false, or a `total` of 0 before the
 *  first progress event lands). */
export function percent(loaded: number, total: number, computable: boolean): number | null {
  if (!computable || total <= 0) return null;
  return Math.min(100, Math.round((loaded / total) * 100));
}

/** The line shown beside an in-flight upload's drop zone: "Uploading
 *  clip.mp4 — 42% of 383 MB" while the browser can measure it, or just
 *  "Uploading clip.mp4…" when it can't — some proxies and dev servers strip
 *  `Content-Length` from a streamed request body. */
export function uploadLabel(
  name: string,
  loaded: number,
  total: number,
  computable: boolean,
): string {
  const pct = percent(loaded, total, computable);
  if (pct === null) return `Uploading ${name}…`;
  return `Uploading ${name} — ${pct}% of ${formatBytes(total)}`;
}

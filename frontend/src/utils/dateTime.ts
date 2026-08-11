const HAS_TIMEZONE_SUFFIX = /(?:z|[+-]\d{2}:?\d{2})$/i;

/**
 * Backend timestamps are stored in UTC. SQLite may return them without an
 * explicit offset, so add the missing UTC marker before formatting them.
 */
export function parseUtcTimestamp(value: string): Date {
  const normalized = HAS_TIMEZONE_SUFFIX.test(value) ? value : `${value}Z`;
  return new Date(normalized);
}

export function formatBeijingDateTime(value?: string): string {
  if (!value) return "—";

  const date = parseUtcTimestamp(value);
  if (Number.isNaN(date.getTime())) return "—";

  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  }).format(date);
}

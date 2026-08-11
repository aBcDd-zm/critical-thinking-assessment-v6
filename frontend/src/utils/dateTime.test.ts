import { describe, expect, it } from "vitest";
import { formatBeijingDateTime, parseUtcTimestamp } from "./dateTime";

describe("Beijing time formatting", () => {
  it("treats a timezone-free backend timestamp as UTC", () => {
    expect(parseUtcTimestamp("2026-08-11T00:44:00").toISOString()).toBe("2026-08-11T00:44:00.000Z");
    expect(formatBeijingDateTime("2026-08-11T00:44:00")).toContain("08:44");
  });

  it("preserves an explicit offset before converting to Beijing time", () => {
    expect(formatBeijingDateTime("2026-08-11T08:44:00+08:00")).toContain("08:44");
  });

  it("returns a placeholder for empty or invalid values", () => {
    expect(formatBeijingDateTime()).toBe("—");
    expect(formatBeijingDateTime("not-a-date")).toBe("—");
  });
});

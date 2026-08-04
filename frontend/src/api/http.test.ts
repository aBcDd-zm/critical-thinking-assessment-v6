import { afterEach, describe, expect, it, vi } from "vitest";

describe("absoluteApiUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("resolves a server /api URL against the current origin when the production base is relative", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "/api/v1");
    const { absoluteApiUrl } = await import("./http");

    expect(absoluteApiUrl("/api/v1/sessions/s-1/turns/2/speech")).toBe(
      `${window.location.origin}/api/v1/sessions/s-1/turns/2/speech`,
    );
  });

  it("keeps non-prefixed speech paths under a relative API base", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "/api/v1");
    const { absoluteApiUrl } = await import("./http");

    expect(absoluteApiUrl("/sessions/s-1/turns/2/speech")).toBe(
      `${window.location.origin}/api/v1/sessions/s-1/turns/2/speech`,
    );
  });
});

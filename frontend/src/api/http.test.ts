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

describe("administrator API transport", () => {
  afterEach(() => {
    document.cookie = "cta_v6_admin_csrf=; Max-Age=0; path=/";
  });

  it("sends the readable CSRF nonce and credentials for unsafe protected admin requests", async () => {
    document.cookie = "cta_v6_admin_csrf=csrf-test-value; path=/";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ review_status: "approved" }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    const { apiRequest } = await import("./http");

    await apiRequest("/admin/sessions/demo/review", { method: "PUT", body: JSON.stringify({ status: "approved" }) });

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.credentials).toBe("include");
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-test-value");
  });

  it("does not require a CSRF nonce to create a login session", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ user: { username: "teacher", display_name: "教师" } }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    const { apiRequest } = await import("./http");

    await apiRequest("/admin/auth/login", { method: "POST", body: JSON.stringify({ username: "teacher", password: "secret" }) });

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.credentials).toBe("include");
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBeNull();
  });

  it("uses the protected idempotent admin report retry endpoint", async () => {
    document.cookie = "cta_v6_admin_csrf=csrf-test-value; path=/";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session: { uuid: "demo", phase: "completed" }, report: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const { finalizeAdminSession } = await import("./admin");

    await finalizeAdminSession("demo");

    expect(fetchMock.mock.calls[0]?.[0]).toContain("/admin/sessions/demo/finalize");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
  });
});

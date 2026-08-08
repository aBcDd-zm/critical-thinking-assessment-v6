import { describe, expect, it, vi } from "vitest";
import {
  acceptClosureSuggestion,
  checkReportReadiness,
  closureSuggestionData,
  completedData,
  createSession,
  finalizingSession,
  finalizeSession,
  submitTurnStream,
} from "./session";
import type { TurnRequest, TurnStreamEvent } from "@/types/contracts";

describe("V6 natural interview NDJSON client", () => {
  it("creates a natural session with only consent and an optional display name", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session: { uuid: "session-v6", phase: "interviewing", turns: [] } }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );

    await createSession({
      consent_version: "v6-natural-interview-2026-08",
      consent_given: true,
      participant: {},
    });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      consent_version: "v6-natural-interview-2026-08",
      consent_given: true,
      participant: {},
    });
  });

  it("uses the idempotent finalize endpoint for an explicit end or scoring retry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        session: { uuid: "session-v6", phase: "completed", turns: [], report_available: true },
        report: { session_uuid: "session-v6", dimensions: [], strengths: [], priorities: [] },
      }), { status: 200, headers: { "content-type": "application/json" } }),
    );

    const fingerprint = "f".repeat(64);
    const result = await finalizeSession("session-v6", {
      evidence_check_id: 12,
      expected_transcript_fingerprint: fingerprint,
      allow_incomplete: true,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toContain("/sessions/session-v6/finalize");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      evidence_check_id: 12,
      expected_transcript_fingerprint: fingerprint,
      allow_incomplete: true,
    });
    expect(result.session.phase).toBe("completed");
  });

  it("requests only the participant-safe aggregate report readiness result", async () => {
    const response = {
      status: "insufficient",
      ready: false,
      cached: true,
      check_id: 12,
      transcript_fingerprint: "e".repeat(64),
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const result = await checkReportReadiness("session-v6");

    expect(fetchMock.mock.calls[0]?.[0]).toContain("/sessions/session-v6/report-readiness");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "POST" });
    expect(result).toEqual(response);
    expect(result).not.toHaveProperty("dimensions");
    expect(result).not.toHaveProperty("scores");
    expect(result).not.toHaveProperty("quotes");
    expect(result).toHaveProperty("transcript_fingerprint", "e".repeat(64));
    expect(result).toHaveProperty("check_id", 12);
  });

  it("retries a failed exact evidence snapshot only when explicitly requested", async () => {
    const response = {
      status: "checking",
      ready: null,
      cached: false,
      check_id: 13,
      transcript_fingerprint: "d".repeat(64),
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 202,
        headers: { "content-type": "application/json" },
      }),
    );

    await checkReportReadiness("session-v6", true);

    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      "/sessions/session-v6/report-readiness?retry_failed=true",
    );
  });

  it("accepts one exact persisted closure suggestion with its transcript fingerprint", async () => {
    const fingerprint = "a".repeat(64);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        session: { uuid: "session-v6", phase: "completed", turns: [], closure_suggestion: null },
        report: { session_uuid: "session-v6", dimensions: [], strengths: [], priorities: [] },
      }), { status: 200, headers: { "content-type": "application/json" } }),
    );

    const result = await acceptClosureSuggestion(
      "session-v6",
      23,
      { expected_transcript_fingerprint: fingerprint },
    );

    expect(fetchMock.mock.calls[0]?.[0]).toContain("/sessions/session-v6/closure-suggestions/23/accept");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      expected_transcript_fingerprint: fingerprint,
    });
    expect(result.session.phase).toBe("completed");
  });

  it("parses a close suggestion without treating it as a finalizing event", () => {
    const fingerprint = "b".repeat(64);
    const event: TurnStreamEvent = {
      event: "session_closure_suggested",
      data: {
        session_uuid: "session-v6",
        closure_turn_id: 17,
        transcript_fingerprint: fingerprint,
        finish_reason: "natural_closure",
      },
    };

    expect(closureSuggestionData(event)).toEqual({
      session_uuid: "session-v6",
      closure_turn_id: 17,
      transcript_fingerprint: fingerprint,
      finish_reason: "natural_closure",
    });
    expect(finalizingSession(event)).toBeNull();
    expect(completedData({
      event: "agent_completed",
      data: {
        turn: { turn_index: 2, role: "assistant", content: "我们可以在这里停一停。" },
        session_action: "suggest_finish",
        finish_reason: "natural_closure",
      },
    })).toMatchObject({ session_action: "suggest_finish", finish_reason: "natural_closure" });
  });

  it("keeps the exact idempotency payload and consumes a natural-close stream", async () => {
    const encoder = new TextEncoder();
    const chunks = [
      '{"event":"user_turn_saved","data":{"turn_index":1}}\n{"event":"agent_',
      'delta","delta":"我听"}\n{"event":"heartbeat"}\n{"event":"agent_delta","delta":"到了"}\n',
      '{"event":"agent_completed","data":{"turn":{"turn_index":2,"role":"assistant","content":"谢谢你愿意说这些。"},"session_action":"finish","finish_reason":"natural_closure"}}\n',
      '{"event":"session_finalizing","data":{"session":{"uuid":"session-1","phase":"finalizing","turns":[]}}}\n',
    ];
    const body = new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
        controller.close();
      },
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { status: 200, headers: { "content-type": "application/x-ndjson" } }),
    );
    const payload: TurnRequest = {
      content: "我会先核实样本范围。",
      client_turn_id: "client-fixed-1",
      input_mode: "voice_edited",
      answer_duration_ms: 3210,
    };
    const events: TurnStreamEvent[] = [];
    await submitTurnStream("session-1", payload, (event) => { events.push(event); });

    expect(events.map((event) => event.event)).toEqual([
      "user_turn_saved",
      "agent_delta",
      "heartbeat",
      "agent_delta",
      "agent_completed",
      "session_finalizing",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(payload);
    expect(completedData(events[4]!)).toMatchObject({
      turn: { content: "谢谢你愿意说这些。" },
      session_action: "finish",
      finish_reason: "natural_closure",
    });
    expect(finalizingSession(events[5]!)).toMatchObject({ uuid: "session-1", phase: "finalizing" });
  });

  it("awaits each async event callback before processing the next event", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(
          '{"event":"agent_delta","delta":"A"}\n{"event":"agent_completed","data":{"turn":{"turn_index":2,"role":"assistant","content":"AB"},"session_action":"continue","finish_reason":null}}\n',
        ));
        controller.close();
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status: 200 }));
    const sequence: string[] = [];
    await submitTurnStream(
      "session-1",
      { content: "answer", client_turn_id: "client-async-1", input_mode: "text", answer_duration_ms: 1 },
      async (event) => {
        sequence.push(`start:${event.event}`);
        await Promise.resolve();
        sequence.push(`end:${event.event}`);
      },
    );
    expect(sequence).toEqual([
      "start:agent_delta",
      "end:agent_delta",
      "start:agent_completed",
      "end:agent_completed",
    ]);
  });

  it("rejects a clean EOF that has no completed or error terminal event", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"event":"user_turn_saved","data":{}}\n'));
        controller.close();
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status: 200 }));
    await expect(
      submitTurnStream(
        "session-1",
        { content: "answer", client_turn_id: "client-incomplete-1", input_mode: "text", answer_duration_ms: 1 },
        vi.fn(),
      ),
    ).rejects.toThrow("完成前中断");
  });

  it("propagates an async handler failure instead of reporting success", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"event":"error","message":"server failed"}\n'));
        controller.close();
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status: 200 }));
    await expect(
      submitTurnStream(
        "session-1",
        { content: "answer", client_turn_id: "client-error-1", input_mode: "text", answer_duration_ms: 1 },
        async () => { throw new Error("event rejected"); },
      ),
    ).rejects.toThrow("event rejected");
  });
});

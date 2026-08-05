import { API_BASE_URL, ApiError, apiRequest } from "./http";
import type {
  AgentCompletedData,
  AssessmentReport,
  CreateSessionRequest,
  CreateSessionResponse,
  FinalizeResponse,
  SessionSnapshot,
  TurnRequest,
  TurnStreamEvent,
} from "@/types/contracts";

export async function createSession(payload: CreateSessionRequest): Promise<CreateSessionResponse> {
  const result = await apiRequest<CreateSessionResponse | (SessionSnapshot & { initial_turn?: CreateSessionResponse["initial_turn"] })>(
    "/sessions",
    { method: "POST", body: JSON.stringify(payload) },
  );
  if ("session" in result) return result;
  return { session: result, initial_turn: result.initial_turn };
}

export const getSession = (uuid: string) =>
  apiRequest<SessionSnapshot>(`/sessions/${encodeURIComponent(uuid)}`);

export const getReport = (uuid: string) =>
  apiRequest<AssessmentReport>(`/sessions/${encodeURIComponent(uuid)}/report`);

export const getReportPdf = (uuid: string) =>
  apiRequest<Blob>(`/sessions/${encodeURIComponent(uuid)}/report.pdf`, {}, "blob");

export const exitSession = (uuid: string) =>
  apiRequest<SessionSnapshot>(`/sessions/${encodeURIComponent(uuid)}/exit`, { method: "POST" });

export async function finalizeSession(uuid: string): Promise<FinalizeResponse> {
  const result = await apiRequest<FinalizeResponse | SessionSnapshot>(
    `/sessions/${encodeURIComponent(uuid)}/finalize`,
    { method: "POST" },
  );
  if ("session" in result) return result;
  return { session: result };
}

function parseEvent(raw: unknown): TurnStreamEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const event = record.event ?? record.type;
  if (typeof event !== "string") return null;
  return {
    event: event as TurnStreamEvent["event"],
    data: record.data,
    delta: typeof record.delta === "string" ? record.delta : undefined,
    message: typeof record.message === "string" ? record.message : undefined,
    code: typeof record.code === "string" ? record.code : undefined,
  };
}

export function completedData(event: TurnStreamEvent): AgentCompletedData | null {
  if (event.event !== "agent_completed" || !event.data || typeof event.data !== "object") return null;
  const data = event.data as Record<string, unknown>;
  const turn = (data.turn ?? data.ai_turn) as AgentCompletedData["turn"] | undefined;
  if (!turn) return null;
  return {
    turn,
    session_action: data.session_action === "finish" ? "finish" : "continue",
    finish_reason: data.finish_reason === "enough_understanding"
      || data.finish_reason === "natural_closure"
      || data.finish_reason === "user_requested"
      || data.finish_reason === "safety_stopped"
      || data.finish_reason === "technical_limit"
      ? data.finish_reason
      : null,
    speech_url: typeof data.speech_url === "string" ? data.speech_url : null,
    session: data.session as SessionSnapshot | undefined,
  };
}

/** The server may freeze the transcript after a natural closing turn. */
export function finalizingSession(event: TurnStreamEvent): SessionSnapshot | null {
  if (event.event !== "session_finalizing" || !event.data || typeof event.data !== "object") return null;
  const data = event.data as Record<string, unknown>;
  const session = data.session ?? data;
  if (!session || typeof session !== "object") return null;
  const snapshot = session as Partial<SessionSnapshot>;
  return typeof snapshot.uuid === "string" && typeof snapshot.phase === "string"
    ? snapshot as SessionSnapshot
    : null;
}

export async function submitTurnStream(
  uuid: string,
  payload: TurnRequest,
  onEvent: (event: TurnStreamEvent) => void | Promise<void>,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/sessions/${encodeURIComponent(uuid)}/turns:stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = body && typeof body.detail === "string"
      ? body.detail
      : body && typeof body.message === "string"
        ? body.message
        : `提交失败（${response.status}）`;
    throw new ApiError(message, response.status, body);
  }
  if (!response.body) throw new ApiError("浏览器未提供流式响应能力", 0);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminalEvent: "agent_completed" | "error" | null = null;

  const processLine = async (line: string) => {
    const clean = line.trim();
    if (!clean) return;
    let raw: unknown;
    try {
      raw = JSON.parse(clean);
    } catch {
      throw new ApiError("服务端返回了无法解析的访谈事件；可使用同一提交编号恢复。", 0);
    }
    const event = parseEvent(raw);
    if (!event) throw new ApiError("服务端返回了缺少事件类型的数据；可使用同一提交编号恢复。", 0);
    if (event.event === "agent_completed" || event.event === "error") terminalEvent = event.event;
    await onEvent(event);
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) await processLine(line);
    if (done) break;
  }
  if (buffer.trim()) await processLine(buffer);
  if (!terminalEvent) {
    throw new ApiError("处理连接在完成前中断；刷新后会使用相同 client_turn_id 恢复，不会重复计入。", 0);
  }
}

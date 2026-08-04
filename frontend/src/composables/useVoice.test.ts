import { describe, expect, it, vi } from "vitest";
import { useVoiceInput } from "./useVoice";

describe("voice input", () => {
  it("only writes an editable transcript and never submits", () => {
    let recognition: FakeRecognition | null = null;
    class FakeRecognition {
      lang = "";
      continuous = false;
      interimResults = false;
      onresult: ((event: never) => void) | null = null;
      onerror = null;
      onend = null;
      constructor() { recognition = this; }
      start = vi.fn();
      stop = vi.fn();
      abort = vi.fn();
    }
    Object.defineProperty(window, "webkitSpeechRecognition", { configurable: true, value: FakeRecognition });
    const transcript = vi.fn();
    const started = vi.fn();
    const voice = useVoiceInput(transcript, started);
    voice.start();
    expect(started).toHaveBeenCalledOnce();
    expect(voice.listening.value).toBe(true);
    recognition!.onresult?.({
      resultIndex: 0,
      results: [{ 0: { transcript: "先核实信息" }, isFinal: true }],
    } as never);
    expect(transcript).toHaveBeenCalledWith("先核实信息");
    expect(recognition!.start).toHaveBeenCalledOnce();
  });
});

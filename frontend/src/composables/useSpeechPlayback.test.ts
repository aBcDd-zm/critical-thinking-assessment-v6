import { describe, expect, it, vi } from "vitest";
import { useSpeechPlayback } from "./useVoice";

class AudioFailure {
  src = "";
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  pause = vi.fn();
  play = vi.fn(() => Promise.reject(new Error("tts unavailable")));
}

class AudioPending {
  src = "";
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  pause = vi.fn();
  play = vi.fn(() => new Promise<void>(() => undefined));
}

describe("speech playback", () => {
  it("falls back to browser speech when the server MP3 cannot play", async () => {
    vi.stubGlobal("Audio", AudioFailure);
    const browserSpeak = vi.spyOn(window.speechSynthesis, "speak").mockImplementation((utterance) => {
      queueMicrotask(() => utterance.onend?.({} as SpeechSynthesisEvent));
    });
    const playback = useSpeechPlayback();
    await playback.speak("下一条问题", "/sessions/s/turns/2/speech");
    expect(browserSpeak).toHaveBeenCalledOnce();
    expect(playback.fallbackUsed.value).toBe(true);
    expect(playback.speaking.value).toBe(false);
  });

  it("settles an in-flight audio promise when recording interrupts playback", async () => {
    vi.stubGlobal("Audio", AudioPending);
    const playback = useSpeechPlayback();
    const inFlight = playback.speak("正在播报", "/sessions/s/turns/2/speech");
    await Promise.resolve();
    expect(playback.speaking.value).toBe(true);
    playback.stop();
    await expect(inFlight).resolves.toBeUndefined();
    expect(playback.speaking.value).toBe(false);
  });
});

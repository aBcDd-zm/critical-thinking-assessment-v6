import { computed, getCurrentInstance, onBeforeUnmount, ref } from "vue";
import { absoluteApiUrl } from "@/api/http";

interface RecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}

interface RecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<RecognitionResultLike>;
}

interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: RecognitionEventLike) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type RecognitionConstructor = new () => RecognitionLike;

export function useVoiceInput(onTranscript: (text: string) => void, onStart?: () => void) {
  const listening = ref(false);
  const error = ref("");
  const finalText = ref("");
  let recognition: RecognitionLike | null = null;

  const Constructor = computed(() => {
    const speechWindow = window as typeof window & {
      SpeechRecognition?: RecognitionConstructor;
      webkitSpeechRecognition?: RecognitionConstructor;
    };
    return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition;
  });
  const supported = computed(() => Boolean(Constructor.value));

  function start(existingText = "") {
    if (!Constructor.value) {
      error.value = "当前浏览器不支持语音转写，请使用文本输入。";
      return;
    }
    onStart?.();
    error.value = "";
    finalText.value = existingText.trim();
    recognition = new Constructor.value();
    recognition.lang = "zh-CN";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = (event) => {
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const item = event.results[index];
        const text = item?.[0]?.transcript ?? "";
        if (item?.isFinal) finalText.value = `${finalText.value}${finalText.value ? " " : ""}${text}`.trim();
        else interim += text;
      }
      onTranscript(`${finalText.value}${interim ? `${finalText.value ? " " : ""}${interim}` : ""}`);
    };
    recognition.onerror = (event) => {
      if (event.error !== "aborted" && event.error !== "no-speech") {
        error.value = `语音转写暂不可用（${event.error}），已保留文本输入。`;
      }
    };
    recognition.onend = () => {
      listening.value = false;
    };
    recognition.start();
    listening.value = true;
  }

  function stop() {
    recognition?.stop();
    listening.value = false;
  }

  if (getCurrentInstance()) onBeforeUnmount(() => recognition?.abort());
  return { listening, supported, error, start, stop };
}

export function useSpeechPlayback() {
  const speaking = ref(false);
  const fallbackUsed = ref(false);
  const error = ref("");
  let audio: HTMLAudioElement | null = null;
  let settlePlayback: (() => void) | null = null;
  let generation = 0;

  function stop() {
    generation += 1;
    if (audio) {
      audio.pause();
      audio.src = "";
      audio = null;
    }
    window.speechSynthesis?.cancel();
    const settle = settlePlayback;
    settlePlayback = null;
    settle?.();
    speaking.value = false;
  }

  function browserSpeak(text: string, currentGeneration: number): Promise<void> {
    return new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (settlePlayback === finish) settlePlayback = null;
        if (generation === currentGeneration) speaking.value = false;
        resolve();
      };
      settlePlayback = finish;
      if (!("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") {
        error.value = "当前浏览器不支持语音播报。";
        finish();
        return;
      }
      fallbackUsed.value = true;
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "zh-CN";
      utterance.rate = 0.98;
      utterance.onend = finish;
      utterance.onerror = () => {
        error.value = "语音播报暂不可用，文字访谈可继续。";
        finish();
      };
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    });
  }

  async function speak(text: string, speechUrl?: string | null): Promise<void> {
    stop();
    const currentGeneration = ++generation;
    error.value = "";
    fallbackUsed.value = false;
    speaking.value = true;
    if (!speechUrl) {
      await browserSpeak(text, currentGeneration);
      return;
    }
    let playbackAudio: HTMLAudioElement | null = null;
    try {
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          if (settlePlayback === finish) settlePlayback = null;
          resolve();
        };
        settlePlayback = finish;
        playbackAudio = new Audio(absoluteApiUrl(speechUrl));
        audio = playbackAudio;
        playbackAudio.onended = finish;
        playbackAudio.onerror = () => {
          if (settled) return;
          settled = true;
          if (settlePlayback === finish) settlePlayback = null;
          reject(new Error("server-tts-failed"));
        };
        playbackAudio.play().catch((cause) => {
          if (settled) return;
          settled = true;
          if (settlePlayback === finish) settlePlayback = null;
          reject(cause);
        });
      });
      if (generation === currentGeneration) speaking.value = false;
      if (audio === playbackAudio) audio = null;
    } catch {
      if (audio === playbackAudio) audio = null;
      if (generation === currentGeneration) await browserSpeak(text, currentGeneration);
    }
  }

  if (getCurrentInstance()) onBeforeUnmount(stop);
  return { speaking, fallbackUsed, error, speak, stop };
}

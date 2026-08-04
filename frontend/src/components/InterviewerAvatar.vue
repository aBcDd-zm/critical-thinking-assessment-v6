<script setup lang="ts">
import type { InterviewerState } from "@/types/contracts";

defineProps<{ state: InterviewerState }>();

const labels: Record<InterviewerState, string> = {
  listening: "访谈官等待并倾听回答",
  thinking: "访谈官正在整理信息",
  speaking: "访谈官正在说话",
};
</script>

<template>
  <div class="interviewer" :class="`is-${state}`" role="img" :aria-label="labels[state]">
    <svg viewBox="0 0 160 160" aria-hidden="true">
      <defs>
        <linearGradient id="halo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#dcebdc" />
          <stop offset="1" stop-color="#e7ded0" />
        </linearGradient>
      </defs>
      <circle class="avatar-halo" cx="80" cy="80" r="72" fill="url(#halo)" />
      <path class="avatar-body" d="M35 148c5-31 20-47 45-47s40 16 45 47" />
      <circle class="avatar-face" cx="80" cy="68" r="35" />
      <path class="avatar-hair" d="M47 66c1-30 16-43 37-43 19 0 32 12 34 34-15-2-26-9-33-20-8 14-20 23-38 29Z" />
      <path class="avatar-eye" d="M64 70h7M90 70h7" />
      <path class="avatar-mouth" d="M70 84c7 5 13 5 20 0" />
      <g class="sound-waves">
        <path d="M126 66c7 6 7 22 0 28" />
        <path d="M136 58c12 11 12 34 0 45" />
      </g>
      <g class="thinking-dots">
        <circle cx="126" cy="38" r="4" />
        <circle cx="138" cy="30" r="5" />
        <circle cx="152" cy="19" r="7" />
      </g>
    </svg>
    <span class="state-label">{{ labels[state] }}</span>
  </div>
</template>

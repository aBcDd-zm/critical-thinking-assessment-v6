<script setup lang="ts">
import { computed } from "vue";
import { DIMENSIONS, type ReportDimension } from "@/types/contracts";
import { publicScoreLabel } from "./scoreFormat";

const props = defineProps<{ dimensions: ReportDimension[] }>();
const center = 120;
const radius = 86;

function point(index: number, value: number): string {
  const angle = -Math.PI / 2 + (index * Math.PI * 2) / 6;
  const scaled = (radius * value) / 5;
  return `${center + Math.cos(angle) * scaled},${center + Math.sin(angle) * scaled}`;
}

const rings = [1, 2, 3, 4, 5].map((level) =>
  DIMENSIONS.map((_, index) => point(index, level)).join(" "),
);
const axes = DIMENSIONS.map((dimension, index) => ({
  ...dimension,
  end: point(index, 5),
  label: point(index, 6.15),
}));
const markers = computed(() => {
  const values = new Map(props.dimensions.map((item) => [item.dimension_key, item]));
  return DIMENSIONS.map((meta, index) => {
    const dimension = values.get(meta.key);
    return {
      key: meta.key,
      status: dimension?.status ?? "unmeasured",
      score: dimension?.status === "sufficient" ? dimension.score : null,
      scorePoint: dimension?.status === "sufficient" && dimension.score !== null ? point(index, dimension.score) : null,
      statusPoint: point(index, 5.45),
    };
  });
});

const scoreArea = computed(() => {
  const values = new Map(props.dimensions.map((item) => [item.dimension_key, item]));
  const points: string[] = [];
  for (const [index, meta] of DIMENSIONS.entries()) {
    const dimension = values.get(meta.key);
    if (dimension?.status !== "sufficient" || dimension.score === null || dimension.score === undefined) {
      return null;
    }
    points.push(point(index, dimension.score));
  }
  return points.join(" ");
});
</script>

<template>
  <svg class="radar-chart" viewBox="0 0 240 240" role="img" aria-label="六维 1 到 5 序数证据等级图；只有证据充分的维度显示等级位置，未评分维度仅显示状态标记">
    <polygon v-for="ring in rings" :key="ring" :points="ring" class="radar-ring" />
    <line v-for="axis in axes" :key="axis.key" :x1="center" :y1="center" :x2="axis.end.split(',')[0]" :y2="axis.end.split(',')[1]" class="radar-axis" />
    <polygon v-if="scoreArea" :points="scoreArea" class="radar-area" />
    <g v-for="marker in markers" :key="`${marker.key}-marker`">
      <circle
        v-if="marker.scorePoint"
        :cx="marker.scorePoint.split(',')[0]"
        :cy="marker.scorePoint.split(',')[1]"
        r="5"
        class="radar-score-dot"
      >
        <title>{{ publicScoreLabel(marker.score) }}</title>
      </circle>
      <circle
        v-else
        :cx="marker.statusPoint.split(',')[0]"
        :cy="marker.statusPoint.split(',')[1]"
        r="3.5"
        class="radar-status-dot"
        :class="`is-${marker.status}`"
      >
        <title>{{ marker.status === "limited" ? "证据有限，不评分" : "未充分测得，不评分" }}</title>
      </circle>
    </g>
    <text v-for="axis in axes" :key="`${axis.key}-label`" :x="axis.label.split(',')[0]" :y="axis.label.split(',')[1]" class="radar-label">{{ axis.name }}</text>
  </svg>
</template>

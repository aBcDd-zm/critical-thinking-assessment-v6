export function publicScoreLabel(score: number | null | undefined): string {
  if (typeof score !== "number" || !Number.isFinite(score)) return "—";
  return `证据等级 ${Number.isInteger(score) ? score : score.toFixed(1)}/5（序数）`;
}

export function averageEvidenceScore(scores: Array<number | null | undefined>): number | null {
  const measured = scores.filter((score): score is number => typeof score === "number" && Number.isFinite(score));
  if (!measured.length) return null;
  return measured.reduce((sum, score) => sum + score, 0) / measured.length;
}

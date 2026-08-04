/**
 * The assessment contract remains a five-level evidence score internally.
 * Participant-facing reports present that fixed scale as a score out of 100.
 */
export function scoreOutOfHundred(score: number | null | undefined): number | null {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  return Math.round(score * 20);
}

export function publicScoreLabel(score: number | null | undefined): string {
  const value = scoreOutOfHundred(score);
  return value === null ? "—" : `${value} 分`;
}

export function averageEvidenceScore(scores: Array<number | null | undefined>): number | null {
  const measured = scores.filter((score): score is number => typeof score === "number" && Number.isFinite(score));
  if (!measured.length) return null;
  return measured.reduce((sum, score) => sum + score, 0) / measured.length;
}

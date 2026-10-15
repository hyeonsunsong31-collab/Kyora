/** Pure helpers shared by the game module and the Node logic tests. */
export function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

export function scoreFor(taps, combo = 1) {
  return taps * Math.max(1, combo);
}

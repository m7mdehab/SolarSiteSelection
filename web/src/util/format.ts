/** Format a possibly-missing number safely. Returns 'N/A' for null/undefined/NaN
 *  so a missing field can never throw (the regression: `.toFixed()` on undefined
 *  unmounted the whole app). */
export function fmt(v: number | null | undefined, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : 'N/A';
}

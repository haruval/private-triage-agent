// Small display helpers mirroring the terminal renderer in src/cli.py:
// same truncation lengths, importance formatting, and color thresholds as
// _print_queue_summary / _render_process_panel / _confidence_bar.

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max).trimEnd() + '…'
}

export function collapseWhitespace(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(' ')
}

/** 9.0 -> "9", 7.5 -> "7.5" — matches _format_importance. */
export function formatImportance(importance: number): string {
  return Number.isInteger(importance) ? String(importance) : importance.toFixed(1)
}

/** ≥8 error/red, ≥6 amber, else neutral — matches _print_queue_summary. */
export function importanceClass(importance: number): string {
  if (importance >= 8) return 'chip-imp-high'
  if (importance >= 6) return 'chip-imp-mid'
  return 'chip-imp-low'
}

/** ≥0.85 green, ≥0.6 amber, else red — matches _confidence_bar. */
export function confidenceClass(confidence: number): string {
  if (confidence >= 0.85) return 'conf-high'
  if (confidence >= 0.6) return 'conf-mid'
  return 'conf-low'
}

// CATEGORY_STYLE from src/cli.py, mapped onto the app palette.
export const CATEGORY_CLASS: Record<string, string> = {
  action_required: 'cat-action',
  needs_reply: 'cat-reply',
  fyi: 'cat-fyi',
  spam: 'cat-spam',
  unclear: 'cat-unclear',
}

export function categoryClass(category: string): string {
  return CATEGORY_CLASS[category] ?? 'cat-other'
}

// PROVENANCE_STYLE from src/cli.py: local = blue, Claude = purple/magenta.
export function provenanceClass(provenance: string): string {
  return provenance === 'Claude' ? 'prov-claude' : 'prov-local'
}

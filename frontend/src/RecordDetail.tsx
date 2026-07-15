// Right pane: the selected record, mirroring the terminal
// _render_process_panel — original message, classification with confidence
// bar, escalation decision (with the anonymized→Claude→rehydrated line when
// Claude was used), and the draft in an editable field. Approve keeps the
// CLI's semantics: an untouched draft records "approve", a changed one
// records "edit"; both persist. A record with no draft can only be rejected.
import type { QueueRecordDTO, ReviewAction } from './api'
import {
  categoryClass,
  confidenceClass,
  formatImportance,
  importanceClass,
  provenanceClass,
} from './format'

interface Props {
  record: QueueRecordDTO
  index: number
  total: number
  draftText: string
  busy: boolean
  onDraftChange: (text: string) => void
  onAction: (record: QueueRecordDTO, action: ReviewAction, draft: string) => void
}

export default function RecordDetail({
  record,
  index,
  total,
  draftText,
  busy,
  onDraftChange,
  onAction,
}: Props) {
  const r = record
  const originalDraft = r.draft ?? ''
  const hasDraft = originalDraft.trim().length > 0
  const edited = hasDraft && draftText !== originalDraft
  const approveAction: ReviewAction = edited ? 'edit' : 'approve'

  return (
    <section className="detail-pane" aria-label="Selected email">
      <div className="detail-header">
        <span className="detail-count">
          [{index}/{total}]
        </span>
        <span className={`chip ${importanceClass(r.importance)}`}>
          importance {formatImportance(r.importance)}
        </span>
        {r.importance_reason && <span className="dim"> — {r.importance_reason}</span>}
      </div>

      <article className={`panel ${categoryClass(r.result.category)}`}>
        <h2 className="panel-title md-typescale-title-large">
          {r.email.subject || '(no subject)'}
        </h2>

        <div className="kv">
          <span className="k">from</span>
          <span>{r.email.from_addr || '(unknown)'}</span>
        </div>
        <div className="section-label">original message:</div>
        <pre className="original-body">{r.email.body_plain || '(empty body)'}</pre>

        <md-divider />

        <div className="kv">
          <span className="k">category</span>
          <span className={`cat-text ${categoryClass(r.result.category)}`}>
            {r.result.category}
          </span>
        </div>
        <div className="kv">
          <span className="k">confidence</span>
          <span className={`conf ${confidenceClass(r.result.confidence)}`}>
            <md-linear-progress value={r.result.confidence} aria-label="confidence" />
            <span className="conf-value">{r.result.confidence.toFixed(2)}</span>
          </span>
        </div>
        <div className="kv">
          <span className="k">summary</span>
          <span>{r.result.summary}</span>
        </div>
        {r.result.action_items.length > 0 && (
          <div className="kv">
            <span className="k">action items</span>
            <ul className="action-items">
              {r.result.action_items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </div>
        )}

        <md-divider />

        <div className="kv">
          <span className="k">escalate</span>
          <span>
            <span className={r.decision.escalate ? 'escalate-true' : 'escalate-false'}>
              {String(r.decision.escalate)}
            </span>
            <span className="dim">  (score {r.decision.score.toFixed(2)})</span>
          </span>
        </div>
        <div className="kv">
          <span className="k">reason</span>
          <span className="dim">{r.decision.reason}</span>
        </div>
        {r.claude_used && (
          <div className="kv">
            <span className="k">delegation</span>
            <span className="dim">
              anonymized → Claude → rehydrated ({r.placeholder_count} placeholder
              {r.placeholder_count === 1 ? '' : 's'})
            </span>
          </div>
        )}
        {r.error && (
          <div className="kv">
            <span className="k">note</span>
            <span className="warning-note">{r.error}</span>
          </div>
        )}

        <md-divider />

        <div className="draft-head">
          <span className="k">draft</span>
          <span className={`chip ${provenanceClass(r.provenance)}`}>{r.provenance}</span>
          {edited && <span className="dim edited-hint">edited — will be recorded as “edit”</span>}
        </div>
        {hasDraft ? (
          <md-outlined-text-field
            className="draft-field"
            type="textarea"
            rows={10}
            label="Draft reply (editable)"
            value={draftText}
            disabled={busy}
            onInput={(e) => onDraftChange(e.currentTarget.value)}
          />
        ) : (
          <div className="dim no-draft">(no draft — local model didn’t propose one)</div>
        )}

        <div className="actions">
          <md-filled-button
            type="button"
            disabled={busy || !hasDraft || !draftText.trim()}
            onClick={() => onAction(r, approveAction, draftText)}
          >
            {edited ? 'Approve edit' : 'Approve'}
          </md-filled-button>
          <md-outlined-button
            type="button"
            disabled={busy}
            onClick={() => onAction(r, 'reject', '')}
          >
            Reject
          </md-outlined-button>
          <span className="dim actions-hint">
            Approve only writes the draft locally, nothing is ever sent.
          </span>
        </div>
      </article>
    </section>
  )
}

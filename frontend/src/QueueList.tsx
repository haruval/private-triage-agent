// Left pane: one row per pending record, mirroring the terminal
// _print_queue_summary — index, importance chip, subject, from, category /
// provenance / escalated badges, one-line summary, up to 3 action items,
// importance reason. All email-derived strings render as text nodes only.
import type { QueueRecordDTO } from './api'
import {
  categoryClass,
  collapseWhitespace,
  formatEmailDate,
  formatImportance,
  importanceClass,
  provenanceClass,
  truncate,
} from './format'

interface Props {
  records: QueueRecordDTO[]
  selectedId: string | null // a record_id, not a Message-ID
  onSelect: (recordId: string) => void
}

export default function QueueList({ records, selectedId, onSelect }: Props) {
  return (
    <nav className="queue-pane" aria-label="Review queue">
      <div className="queue-title md-typescale-title-medium">
        {records.length} email{records.length === 1 ? '' : 's'} awaiting
        review
      </div>
      <md-list className="queue-list">
        {records.map((r, i) => (
          <md-list-item
            key={r.record_id}
            type="button"
            className={r.record_id === selectedId ? 'queue-row selected' : 'queue-row'}
            aria-current={r.record_id === selectedId ? 'true' : undefined}
            onClick={() => onSelect(r.record_id)}
          >
            <div slot="headline" className="row-head">
              <span className="row-index">{i + 1}</span>
              <span className={`chip importance-circle ${importanceClass(r.importance)}`}>
                {formatImportance(r.importance)}
              </span>
              <span className="row-subject">{r.email.subject || '(no subject)'}</span>
            </div>
            <div slot="supporting-text" className="row-body">
              <div className="row-meta">
                <span className="dim">from {r.email.from_addr || '(unknown)'}</span>
                {formatEmailDate(r.email.date) && (
                  <span className="dim">{formatEmailDate(r.email.date)}</span>
                )}
              </div>
              <div className="row-meta">
                <span className={`chip ${categoryClass(r.result.category)}`}>
                  {r.result.category}
                </span>
                <span className={`chip ${provenanceClass(r.provenance)}`}>
                  draft: {r.provenance}
                </span>
                {r.decision.escalate && <span className="chip chip-escalated">escalated</span>}
              </div>
              <div className="row-summary">
                {truncate(collapseWhitespace(r.result.summary), 220)}
              </div>
              {r.result.action_items.slice(0, 3).map((item, j) => (
                <div key={j} className="row-item">
                  • {truncate(collapseWhitespace(item), 110)}
                </div>
              ))}
              {r.importance_reason && (
                <div className="row-reason">({truncate(r.importance_reason, 110)})</div>
              )}
            </div>
          </md-list-item>
        ))}
      </md-list>
    </nav>
  )
}

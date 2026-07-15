// The single-page review app: a top bar with the two leading actions
// (Upload .mbox, Connect IMAP), the review queue as the main view, and the
// IMAP settings as a simple state-switched second view (no router). The
// queue polls /api/queue every 15s; approving/rejecting advances to the
// next pending record, exactly like the terminal `review` loop.
import { useCallback, useEffect, useRef, useState } from 'react'

import type { QueueRecordDTO, ReviewAction } from './api'
import { fetchQueue, importMbox, postReview } from './api'
import QueueList from './QueueList'
import RecordDetail from './RecordDetail'
import SettingsView from './SettingsView'

const POLL_MS = 15_000

type View = 'queue' | 'settings'

export default function App() {
  const [view, setView] = useState<View>('queue')
  const [records, setRecords] = useState<QueueRecordDTO[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [apiError, setApiError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  const showToast = useCallback((message: string) => {
    setToast(message)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 7000)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const list = await fetchQueue()
      setRecords(list)
      setApiError(null)
    } catch (err) {
      setApiError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const id = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(id)
  }, [refresh])

  const list = records ?? []
  const selected = list.find((r) => r.email.id === selectedId) ?? list[0] ?? null

  const handleAction = useCallback(
    async (record: QueueRecordDTO, action: ReviewAction, draft: string) => {
      setBusy(true)
      try {
        const resp = await postReview(record.email.id, action, draft)
        const parts: string[] = []
        if (action === 'reject') {
          parts.push('rejected — nothing saved')
        } else {
          parts.push(`saved → ${resp.saved_path ?? '?'}`)
        }
        if (resp.note) parts.push(resp.note)
        if (resp.warning) parts.push(`warning: ${resp.warning}`)
        showToast(parts.join('  •  '))

        // Drop the reviewed record locally and advance to the next one.
        const idx = list.findIndex((r) => r.email.id === record.email.id)
        const next = list.filter((r) => r.email.id !== record.email.id)
        const nextSelected =
          next.length === 0
            ? null
            : next[Math.min(Math.max(idx, 0), next.length - 1)].email.id
        setRecords(next)
        setSelectedId(nextSelected)
        setEdits((prev) => {
          const rest = { ...prev }
          delete rest[record.email.id]
          return rest
        })
        void refresh()
      } catch (err) {
        showToast(`error: ${err instanceof Error ? err.message : String(err)}`)
      } finally {
        setBusy(false)
      }
    },
    [list, refresh, showToast],
  )

  const handleUploadMbox = useCallback(async () => {
    setUploading(true)
    try {
      const resp = await importMbox()
      if (resp.cancelled) {
        showToast('Upload cancelled')
      } else {
        showToast(
          `${resp.filename ?? 'Selected .mbox'} copied to data/inbox — run ` +
            '`python -m src.cli start` to process it',
        )
      }
    } catch (err) {
      showToast(`error: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setUploading(false)
    }
  }, [showToast])

  return (
    <div className="app">
      <header className="topbar">
        <span className="app-title md-typescale-title-large">private triage agent</span>
        <md-filled-tonal-button
          type="button"
          className="topbar-action flat-tonal-action"
          disabled={uploading}
          onClick={() => void handleUploadMbox()}
        >
          {uploading ? 'Selecting…' : 'Upload .mbox'}
        </md-filled-tonal-button>
        {view === 'queue' ? (
          <md-filled-tonal-button
            type="button"
            className="topbar-action flat-tonal-action"
            onClick={() => setView('settings')}
          >
            Connect IMAP
          </md-filled-tonal-button>
        ) : (
          <md-filled-tonal-button
            type="button"
            className="topbar-action"
            onClick={() => setView('queue')}
          >
            ← Back to queue
          </md-filled-tonal-button>
        )}
        <span className="spacer" />
        {view === 'queue' && (
          <>
            {records !== null && (
              <span className="dim pending-count">{list.length} pending</span>
            )}
            <md-filled-tonal-button
              type="button"
              className="topbar-action flat-tonal-action"
              onClick={() => void refresh()}
            >
              Refresh
            </md-filled-tonal-button>
          </>
        )}
      </header>

      {apiError && (
        <div className="api-error" role="alert">
          API unreachable ({apiError}) — is the backend running? Start it with
          <code> make api</code>.
        </div>
      )}

      {view === 'settings' ? (
        <SettingsView showToast={showToast} />
      ) : records === null ? (
        <div className="empty-state dim">Loading queue…</div>
      ) : list.length === 0 ? (
        <div className="empty-state">
          <p className="md-typescale-title-medium">
            Nothing to review — the queue is empty.
          </p>
          <p className="dim">
            Run <code>python -m src.cli start</code> (or <code>start-imap</code>) to
            process new mail.
          </p>
          <md-outlined-button type="button" onClick={() => void refresh()}>
            Refresh
          </md-outlined-button>
        </div>
      ) : (
        <main className="main">
          <QueueList
            records={list}
            selectedId={selected?.email.id ?? null}
            onSelect={setSelectedId}
          />
          {selected && (
            <RecordDetail
              record={selected}
              index={list.indexOf(selected) + 1}
              total={list.length}
              draftText={edits[selected.email.id] ?? selected.draft ?? ''}
              busy={busy}
              onDraftChange={(text) =>
                setEdits((prev) => ({ ...prev, [selected.email.id]: text }))
              }
              onAction={(rec, action, draft) => void handleAction(rec, action, draft)}
            />
          )}
        </main>
      )}

      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  )
}

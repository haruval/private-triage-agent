// The single-page review app: a top bar with the two leading actions
// (Upload .mbox, Connect IMAP), the review queue as the main view, and the
// IMAP settings as a simple state-switched second view (no router). The
// queue polls /api/queue every 15s; approving/rejecting advances to the
// next pending record, exactly like the terminal `review` loop.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type {
  ProcessingSource,
  ProcessingStatus,
  QueueRecordDTO,
  ReviewAction,
} from './api'
import {
  fetchProcessingStatus,
  fetchQueue,
  importMbox,
  postReview,
  startProcessing,
} from './api'
import QueueList from './QueueList'
import RecordDetail from './RecordDetail'
import SettingsView from './SettingsView'

const POLL_MS = 15_000
const PROCESS_POLL_MS = 2_000

type View = 'queue' | 'settings'

export default function App() {
  const [view, setView] = useState<View>('queue')
  const [records, setRecords] = useState<QueueRecordDTO[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null)
  const [apiError, setApiError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<number | undefined>(undefined)
  const processingRef = useRef<ProcessingStatus | null>(null)
  const processInitialized = useRef(false)
  const handledProcessId = useRef<string | null>(null)

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

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const next = await fetchProcessingStatus()
        if (cancelled) return
        const current = processingRef.current
        if (current?.status === 'running' && next.id !== current.id) return
        processingRef.current = next
        setProcessing(next)
        if (!processInitialized.current) {
          processInitialized.current = true
          if (next.status !== 'running') handledProcessId.current = next.id
          return
        }
        if (
          next.id &&
          next.status !== 'running' &&
          next.status !== 'idle' &&
          handledProcessId.current !== next.id
        ) {
          handledProcessId.current = next.id
          if (next.status === 'succeeded') {
            showToast('Mail processing complete — review queue refreshed')
            void refresh()
          } else {
            showToast(`error: ${next.message}`)
          }
        }
      } catch {
        // Queue polling already reports API connectivity failures prominently.
      }
    }
    void poll()
    const id = window.setInterval(() => void poll(), PROCESS_POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [refresh, showToast])

  const list = useMemo(() => records ?? [], [records])
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

  const beginProcessing = useCallback(
    async (source: ProcessingSource, days = 7) => {
      const next = await startProcessing(source, days)
      processInitialized.current = true
      handledProcessId.current = next.status === 'running' ? null : next.id
      processingRef.current = next
      setProcessing(next)
      if (next.status === 'running') {
        showToast(
          source === 'imap'
            ? 'Fetching and processing unread IMAP mail…'
            : 'Processing uploaded .mbox mail…',
        )
      } else if (next.status === 'succeeded') {
        showToast('Mail processing complete — review queue refreshed')
        await refresh()
      } else {
        throw new Error(next.message)
      }
    },
    [refresh, showToast],
  )

  const handleUploadMbox = useCallback(async () => {
    setUploading(true)
    try {
      const resp = await importMbox()
      if (resp.cancelled) {
        showToast('Upload cancelled')
      } else {
        showToast(`${resp.filename ?? 'Selected .mbox'} copied to data/inbox`)
        await beginProcessing('mbox')
      }
    } catch (err) {
      showToast(`error: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setUploading(false)
    }
  }, [beginProcessing, showToast])

  const handleStartImap = useCallback(
    async (days: number) => {
      await beginProcessing('imap', days)
      setView('queue')
    },
    [beginProcessing],
  )

  const isProcessing = processing?.status === 'running'

  return (
    <div className="app">
      <header className="topbar">
        <span className="app-title md-typescale-title-large">private triage agent</span>
        <md-filled-tonal-button
          type="button"
          className="topbar-action flat-tonal-action"
          disabled={uploading || isProcessing}
          onClick={() => void handleUploadMbox()}
        >
          {uploading ? 'Selecting…' : 'Upload .mbox'}
        </md-filled-tonal-button>
        {view === 'queue' ? (
          <md-filled-tonal-button
            type="button"
            className="topbar-action flat-tonal-action"
            disabled={isProcessing}
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

      {isProcessing && processing && (
        <div className="processing-banner" role="status">
          <span>{processing.message}</span>
          <md-linear-progress indeterminate />
          <span className="dim">Detailed progress is also visible in the API terminal.</span>
        </div>
      )}

      {apiError && (
        <div className="api-error" role="alert">
          API unreachable ({apiError}) — is the backend running? Start it with
          <code> make api</code>.
        </div>
      )}

      {view === 'settings' ? (
        <SettingsView
          showToast={showToast}
          processing={isProcessing}
          onStartImap={handleStartImap}
        />
      ) : records === null ? (
        <div className="empty-state dim">Loading queue…</div>
      ) : list.length === 0 ? (
        <div className="empty-state">
          <p className="md-typescale-title-medium">
            Nothing to review — the queue is empty.
          </p>
          <p className="dim">
            Upload an .mbox file or connect IMAP to fetch and process new mail.
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

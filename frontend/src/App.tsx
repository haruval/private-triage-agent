// The single-page review app: a top bar with the leading actions, the review
// queue as the main view, and processing/IMAP settings in an Options dialog.
// The queue polls /api/queue every 15s; approving/rejecting advances to the
// next pending record, exactly like the terminal `review` loop. Records are
// keyed by the opaque record_id — the same Message-ID can be pending once
// per IMAP account, so email.id is display-only.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type {
  Anonymizer,
  ProcessingOptions,
  ProcessingSource,
  ProcessingStatus,
  QueueRecordDTO,
  ReviewAction,
} from './api'
import {
  DEFAULT_PROCESSING_OPTIONS,
  fetchImapSettings,
  fetchProcessingStatus,
  fetchQueue,
  importMbox,
  postReview,
  resetQueue,
  startProcessing,
} from './api'
import type { MdSelectElement, MdTextFieldElement } from './declarations'
import QueueList from './QueueList'
import RecordDetail from './RecordDetail'
import SettingsView from './SettingsView'
import settingsIcon from './assets/settings.svg'

const POLL_MS = 15_000
const PROCESS_POLL_MS = 2_000
const MAX_PROCESS_LIMIT = 10_000

type OptionsSection = 'imap' | 'advanced' | 'reset'

export default function App() {
  const [records, setRecords] = useState<QueueRecordDTO[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null)
  const [options, setOptions] = useState<ProcessingOptions>(DEFAULT_PROCESSING_OPTIONS)
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [optionsSection, setOptionsSection] = useState<OptionsSection>('advanced')
  const [imapUsernameFilled, setImapUsernameFilled] = useState(false)
  const [resetting, setResetting] = useState(false)
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

  const handleImapUsernameChange = useCallback((username: string) => {
    setImapUsernameFilled(username.trim() !== '')
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
    fetchImapSettings()
      .then((settings) => {
        if (!cancelled) setImapUsernameFilled(settings.user.trim() !== '')
      })
      .catch(() => {
        // The queue request reports API connectivity errors; this optional
        // visual hint can remain in its unconfigured state when unavailable.
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const next = await fetchProcessingStatus()
        if (cancelled) return
        const current = processingRef.current
        processingRef.current = next
        setProcessing(next)
        if (!processInitialized.current) {
          processInitialized.current = true
          if (next.status !== 'running') handledProcessId.current = next.id
          return
        }
        if (current?.status === 'running' && next.id !== current.id) {
          // The job we were watching is gone. The API rejects overlapping
          // jobs, so a running job can only vanish when the server restarted
          // mid-run — adopt the server's state instead of waiting forever
          // for a dead job, and tell the user what happened.
          if (next.status !== 'running') handledProcessId.current = next.id
          showToast('Processing was interrupted (API restarted) — the queue may be partial')
          void refresh()
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
            showToast('Mail processing complete - review queue refreshed')
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
  const selected = list.find((r) => r.record_id === selectedId) ?? list[0] ?? null

  const handleAction = useCallback(
    async (record: QueueRecordDTO, action: ReviewAction, draft: string) => {
      setBusy(true)
      try {
        const resp = await postReview(record.record_id, action, draft)
        const parts: string[] = []
        if (action === 'reject') {
          parts.push('rejected - nothing saved')
        } else {
          parts.push(`saved → ${resp.saved_path ?? '?'}`)
        }
        if (resp.note) parts.push(resp.note)
        if (resp.warning) parts.push(`warning: ${resp.warning}`)
        showToast(parts.join('  •  '))

        // Drop the reviewed record locally and advance to the next one.
        const idx = list.findIndex((r) => r.record_id === record.record_id)
        const next = list.filter((r) => r.record_id !== record.record_id)
        const nextSelected =
          next.length === 0
            ? null
            : next[Math.min(Math.max(idx, 0), next.length - 1)].record_id
        setRecords(next)
        setSelectedId(nextSelected)
        setEdits((prev) => {
          const rest = { ...prev }
          delete rest[record.record_id]
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
      const next = await startProcessing(source, days, options)
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
    [options, refresh, showToast],
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
      setOptionsOpen(false)
    },
    [beginProcessing],
  )

  // Toolbar shortcut once IMAP is configured: fetch/process with the default
  // window, no dialog. Errors surface as a toast since no form catches them.
  const handleRefreshImap = useCallback(async () => {
    try {
      await beginProcessing('imap')
    } catch (err) {
      showToast(`error: ${err instanceof Error ? err.message : String(err)}`)
    }
  }, [beginProcessing, showToast])

  const handleReset = useCallback(async () => {
    setResetting(true)
    try {
      const resp = await resetQueue()
      setOptionsOpen(false)
      setRecords([])
      setSelectedId(null)
      setEdits({})
      showToast(
        `Queue reset: ${resp.processed_deleted} processed record(s) and ` +
          `${resp.reviewed_deleted} review decision(s) deleted; approved ` +
          'drafts and session logs are kept',
      )
      void refresh()
    } catch (err) {
      showToast(`error: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setResetting(false)
    }
  }, [refresh, showToast])

  const isProcessing = processing?.status === 'running'
  const optionsCustomized =
    options.limit !== null ||
    options.anonymizer !== DEFAULT_PROCESSING_OPTIONS.anonymizer ||
    options.task.trim() !== ''

  return (
    <div className="app">
      <header className="topbar">
        <span className="app-title md-typescale-title-large">private triage agent</span>
        <md-filled-tonal-button
          type="button"
          className="app-action-shape flat-tonal-action topbar-upload-action"
          disabled={uploading || isProcessing}
          onClick={() => void handleUploadMbox()}
        >
          {uploading ? 'Selecting…' : 'Upload .mbox'}
        </md-filled-tonal-button>
        <md-filled-tonal-button
          type="button"
          className="app-action-shape flat-tonal-action topbar-imap-action"
          disabled={uploading || isProcessing}
          onClick={() => {
            if (imapUsernameFilled) {
              void handleRefreshImap()
            } else {
              setOptionsSection('imap')
              setOptionsOpen(true)
            }
          }}
        >
          {imapUsernameFilled ? 'Refresh IMAP' : 'Connect IMAP'}
        </md-filled-tonal-button>
        <md-filled-tonal-button
          type="button"
          className="app-action-shape flat-tonal-action topbar-options-action"
          disabled={isProcessing}
          aria-label={optionsCustomized ? 'Options (customized)' : 'Options'}
          title={optionsCustomized ? 'Options (customized)' : 'Options'}
          onClick={() => {
            setOptionsSection('advanced')
            setOptionsOpen(true)
          }}
        >
          <img slot="icon" className="settings-icon" src={settingsIcon} alt="" />
        </md-filled-tonal-button>
      </header>

      {isProcessing && processing && (
        <div className="processing-banner" role="status">
          <span>{processing.message}</span>
          {processing.progress_total !== null && processing.progress_total > 0 ? (
            <>
              <md-linear-progress
                value={(processing.progress_done ?? 0) / processing.progress_total}
              />
              <span className="dim">
                {processing.progress_done ?? 0} of {processing.progress_total} email
                {processing.progress_total === 1 ? '' : 's'} processed
              </span>
            </>
          ) : (
            <>
              <md-circular-progress indeterminate />
              <span className="dim">
                Detailed progress is also visible in the API terminal.
              </span>
            </>
          )}
        </div>
      )}

      {apiError && (
        <div className="api-error" role="alert">
          API unreachable ({apiError}) - is the backend running? Start it with
          <code> make api</code>.
        </div>
      )}

      {records === null ? (
        <div className="empty-state dim">Loading queue…</div>
      ) : list.length === 0 ? (
        <div className="empty-state">
          <p className="md-typescale-title-medium">
            Nothing to review, the queue is empty.
          </p>
          <p className="dim">
            Upload an .mbox file or connect IMAP to fetch and process new mail.
          </p>
          <md-filled-tonal-button
            type="button"
            className="app-action-shape flat-tonal-action"
            onClick={() => void refresh()}
          >
            Refresh
          </md-filled-tonal-button>
        </div>
      ) : (
        <main className="main">
          <QueueList
            records={list}
            selectedId={selected?.record_id ?? null}
            onSelect={setSelectedId}
          />
          {selected && (
            <RecordDetail
              record={selected}
              index={list.indexOf(selected) + 1}
              total={list.length}
              draftText={edits[selected.record_id] ?? selected.draft ?? ''}
              busy={busy}
              onDraftChange={(text) =>
                setEdits((prev) => ({ ...prev, [selected.record_id]: text }))
              }
              onAction={(rec, action, draft) => void handleAction(rec, action, draft)}
            />
          )}
        </main>
      )}

      {optionsOpen && (
        <OptionsDialog
          options={options}
          initialSection={optionsSection}
          onSave={(next) => {
            setOptions(next)
            setOptionsOpen(false)
            showToast('Processing options saved for this session')
          }}
          onClose={() => setOptionsOpen(false)}
          onReset={handleReset}
          showToast={showToast}
          onStartImap={handleStartImap}
          onImapUsernameChange={handleImapUsernameChange}
          processing={isProcessing}
          resetting={resetting}
        />
      )}

      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  )
}

// Options groups IMAP connection settings, advanced start/start-imap flags,
// and the destructive queue reset into separate sidebar sections. Advanced
// field edits are local until Save. Router/eval flags stay terminal-only.
const ANONYMIZERS: { id: Anonymizer; label: string; hint: string }[] = [
  { id: 'regex', label: 'regex', hint: 'fixed-shape PII only (fastest)' },
  { id: 'regex+ner', label: 'regex + NER', hint: 'adds named entities (no coreference)' },
  { id: 'combined', label: 'combined', hint: 'regex + NER + coreference (default)' },
]

function OptionsDialog({
  options,
  initialSection,
  onSave,
  onClose,
  onReset,
  showToast,
  onStartImap,
  onImapUsernameChange,
  processing,
  resetting,
}: {
  options: ProcessingOptions
  initialSection: OptionsSection
  onSave: (next: ProcessingOptions) => void
  onClose: () => void
  onReset: () => Promise<void>
  showToast: (message: string) => void
  onStartImap: (days: number) => Promise<void>
  onImapUsernameChange: (username: string) => void
  processing: boolean
  resetting: boolean
}) {
  const [section, setSection] = useState<OptionsSection>(initialSection)
  const [limitText, setLimitText] = useState(
    options.limit === null ? '' : String(options.limit),
  )
  const [anonymizer, setAnonymizer] = useState<Anonymizer>(options.anonymizer)
  const [task, setTask] = useState(options.task)
  const [error, setError] = useState<string | null>(null)

  const handleSave = () => {
    const trimmed = limitText.trim()
    let limit: number | null = null
    if (trimmed !== '') {
      const parsed = Number(trimmed)
      if (
        !Number.isInteger(parsed) ||
        parsed < 1 ||
        parsed > MAX_PROCESS_LIMIT
      ) {
        setError(`Processing limit must be a whole number from 1 to ${MAX_PROCESS_LIMIT}.`)
        return
      }
      limit = parsed
    }
    onSave({ limit, anonymizer, task: task.trim() })
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="options-dialog-title"
    >
      <div className="modal-card options-card">
        <header className="options-dialog-header">
          <h3 id="options-dialog-title" className="md-typescale-title-medium">
            Options
          </h3>
          <button
            type="button"
            className="options-close-button"
            aria-label="Close options"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="options-layout">
          <nav className="options-sidebar" aria-label="Options sections">
            <button
              type="button"
              className={`options-nav-button ${section === 'imap' ? 'active' : ''}`}
              aria-current={section === 'imap' ? 'page' : undefined}
              onClick={() => setSection('imap')}
            >
              Connect IMAP
            </button>
            <button
              type="button"
              className={`options-nav-button ${section === 'advanced' ? 'active' : ''}`}
              aria-current={section === 'advanced' ? 'page' : undefined}
              onClick={() => setSection('advanced')}
            >
              Advanced
            </button>
            <button
              type="button"
              className={`options-nav-button ${section === 'reset' ? 'active' : ''}`}
              aria-current={section === 'reset' ? 'page' : undefined}
              onClick={() => setSection('reset')}
            >
              Reset
            </button>
          </nav>

          <section className="options-content">
            {section === 'imap' ? (
              <SettingsView
                showToast={showToast}
                processing={processing}
                onStartImap={onStartImap}
                onUsernameChange={onImapUsernameChange}
                embedded
              />
            ) : section === 'advanced' ? (
              <>
                <h4 className="md-typescale-title-medium">Advanced</h4>
                <p className="dim">
                  Applied to every processing run started from this page (mbox
                  uploads and IMAP). Leave everything as-is for the defaults.
                </p>
                <div className="modal-form">
                  <md-outlined-text-field
                    label="Processing limit"
                    type="number"
                    min="1"
                    max={String(MAX_PROCESS_LIMIT)}
                    value={limitText}
                    placeholder="all new mail"
                    supporting-text="process at most N new emails per run"
                    onInput={(e) =>
                      setLimitText((e.currentTarget as MdTextFieldElement).value)
                    }
                  />
                  <md-outlined-select
                    label="Anonymizer"
                    value={anonymizer}
                    onInput={(e) =>
                      setAnonymizer(
                        (e.currentTarget as MdSelectElement).value as Anonymizer,
                      )
                    }
                  >
                    {ANONYMIZERS.map((a) => (
                      <md-select-option
                        key={a.id}
                        value={a.id}
                        selected={a.id === anonymizer}
                      >
                        <div slot="headline">{a.label}</div>
                        <div slot="supporting-text">{a.hint}</div>
                      </md-select-option>
                    ))}
                  </md-outlined-select>
                  <md-outlined-text-field
                    label="Task instruction"
                    value={task}
                    placeholder="default: draft a concise, professional reply"
                    supporting-text="one line, sent to Claude for escalated emails (placeholders only, never raw PII)"
                    onInput={(e) =>
                      setTask((e.currentTarget as MdTextFieldElement).value)
                    }
                  />
                </div>
                {error && (
                  <div className="settings-status settings-status-error" role="alert">
                    {error}
                  </div>
                )}
                <div className="modal-actions">
                  <md-outlined-button
                    type="button"
                    className="app-action-shape"
                    onClick={onClose}
                  >
                    Cancel
                  </md-outlined-button>
                  <md-filled-button
                    type="button"
                    className="app-action-shape"
                    onClick={handleSave}
                  >
                    Save options
                  </md-filled-button>
                </div>
              </>
            ) : (
              <>
                <h4 className="md-typescale-title-medium">Reset</h4>
                <p>
                  Reset the review queue so the next processing run treats every
                  email as new, including everything already reviewed.
                </p>
                <div className="reset-warning">
                  This permanently deletes the processed and reviewed ledgers.
                  Approved drafts and session logs are not touched.
                </div>
                <div className="modal-actions">
                  <md-outlined-button
                    type="button"
                    className="app-action-shape"
                    disabled={resetting}
                    onClick={onClose}
                  >
                    Cancel
                  </md-outlined-button>
                  <md-filled-button
                    type="button"
                    className="app-action-shape"
                    disabled={processing || resetting}
                    onClick={() => void onReset()}
                  >
                    {resetting ? 'Resetting…' : 'Reset queue'}
                  </md-filled-button>
                </div>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

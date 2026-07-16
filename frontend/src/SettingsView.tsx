// IMAP settings view. Fields match the existing env contract exactly
// (IMAP_HOST / IMAP_USER / IMAP_PASS / IMAP_FOLDER / IMAP_DRAFTS_FOLDER —
// no port field, the loader uses IMAP4_SSL defaults). The password is write-only: the API
// reports "set"/"unset" and this form only ever sends a new value the user
// typed; leaving it blank keeps the saved one.
import { useEffect, useState } from 'react'

import {
  fetchImapSettings,
  resetImapSettings,
  saveImapSettings,
  testImapSettings,
} from './api'
import type { MdSelectElement, MdTextFieldElement } from './declarations'

const PROVIDERS = [
  {
    id: 'gmail',
    label: 'Gmail',
    host: 'imap.gmail.com',
    draftsFolder: '[Gmail]/Drafts',
  },
  {
    id: 'outlook',
    label: 'Outlook / Office 365',
    host: 'outlook.office365.com',
    draftsFolder: 'Drafts',
  },
  { id: 'icloud', label: 'iCloud Mail', host: 'imap.mail.me.com', draftsFolder: 'Drafts' },
  { id: 'yahoo', label: 'Yahoo Mail', host: 'imap.mail.yahoo.com', draftsFolder: 'Draft' },
  { id: 'custom', label: 'Custom…', host: '', draftsFolder: 'Drafts' },
] as const

type ProviderId = (typeof PROVIDERS)[number]['id']

function providerForHost(host: string): ProviderId {
  const match = PROVIDERS.find((p) => p.id !== 'custom' && p.host === host)
  if (match) return match.id
  return host ? 'custom' : 'gmail'
}

interface Status {
  kind: 'idle' | 'busy' | 'ok' | 'error'
  message: string
}

interface Props {
  showToast: (message: string) => void
  processing: boolean
  onStartImap: (days: number) => Promise<void>
  onUsernameChange: (username: string) => void
  embedded?: boolean
}

export default function SettingsView({
  showToast,
  processing,
  onStartImap,
  onUsernameChange,
  embedded = false,
}: Props) {
  const [loaded, setLoaded] = useState(false)
  const [provider, setProvider] = useState<ProviderId>('gmail')
  const [host, setHost] = useState('imap.gmail.com')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [folder, setFolder] = useState('INBOX')
  const [draftsFolder, setDraftsFolder] = useState('[Gmail]/Drafts')
  const [days, setDays] = useState('7')
  const [passwordSaved, setPasswordSaved] = useState<'set' | 'unset'>('unset')
  const [status, setStatus] = useState<Status>({ kind: 'idle', message: '' })

  useEffect(() => {
    let cancelled = false
    fetchImapSettings()
      .then((s) => {
        if (cancelled) return
        const resolvedHost = s.host || 'imap.gmail.com'
        const p = providerForHost(resolvedHost)
        const defaults = PROVIDERS.find((candidate) => candidate.id === p)
        const resolvedDrafts = s.host
          ? s.drafts_folder || defaults?.draftsFolder || 'Drafts'
          : '[Gmail]/Drafts'
        setProvider(p)
        setHost(resolvedHost)
        setUser(s.user)
        onUsernameChange(s.user)
        setFolder(s.folder || 'INBOX')
        setDraftsFolder(resolvedDrafts)
        setPasswordSaved(s.password)
        setLoaded(true)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setStatus({
          kind: 'error',
          message: err instanceof Error ? err.message : String(err),
        })
        setLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [onUsernameChange])

  const form = {
    host: host.trim(),
    user: user.trim(),
    password,
    folder: folder.trim(),
    drafts_folder: draftsFolder.trim(),
  }
  const busy = status.kind === 'busy' || processing

  const handleProvider = (id: string) => {
    const p = PROVIDERS.find((x) => x.id === id)
    if (!p) return
    setProvider(p.id)
    setHost(p.host)
    setDraftsFolder(p.draftsFolder)
  }

  const handleTest = async () => {
    setStatus({ kind: 'busy', message: 'connecting (read-only)…' })
    try {
      const resp = await testImapSettings(form)
      setStatus(
        resp.ok
          ? { kind: 'ok', message: resp.message ?? 'connected' }
          : { kind: 'error', message: resp.error ?? 'connection failed' },
      )
    } catch (err) {
      setStatus({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    }
  }

  const handleSave = async () => {
    setStatus({ kind: 'busy', message: 'saving…' })
    try {
      const resp = await saveImapSettings(form)
      setPasswordSaved(resp.password)
      setPassword('')
      setStatus({ kind: 'ok', message: 'saved to .env (file mode 0600)' })
      showToast('IMAP settings saved to .env')
    } catch (err) {
      setStatus({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    }
  }

  const handleReset = async () => {
    setStatus({ kind: 'busy', message: 'resetting IMAP settings…' })
    try {
      await resetImapSettings()
      setProvider('gmail')
      setHost('imap.gmail.com')
      setUser('')
      setPassword('')
      setFolder('INBOX')
      setDraftsFolder('[Gmail]/Drafts')
      setDays('7')
      setPasswordSaved('unset')
      onUsernameChange('')
      setStatus({ kind: 'ok', message: 'IMAP settings reset in .env' })
      showToast('IMAP settings reset to defaults')
    } catch (err) {
      setStatus({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    }
  }

  const handleSaveAndProcess = async () => {
    const parsedDays = Number(days)
    if (!Number.isInteger(parsedDays) || parsedDays < 1 || parsedDays > 365) {
      setStatus({ kind: 'error', message: 'Days to fetch must be from 1 to 365.' })
      return
    }
    setStatus({ kind: 'busy', message: 'saving settings…' })
    try {
      const resp = await saveImapSettings(form)
      setPasswordSaved(resp.password)
      setPassword('')
      setStatus({ kind: 'busy', message: 'starting the mail pipeline…' })
      await onStartImap(parsedDays)
      setStatus({ kind: 'ok', message: 'processing started; progress appears above' })
    } catch (err) {
      setStatus({
        kind: 'error',
        message: err instanceof Error ? err.message : String(err),
      })
    }
  }

  return (
    <section
      className={`settings-pane ${embedded ? 'settings-pane-embedded' : ''}`}
      aria-label="IMAP settings"
    >
      {embedded ? (
        <h4 className="md-typescale-title-medium">Connect IMAP</h4>
      ) : (
        <h2 className="md-typescale-headline-small">Connect email over IMAP</h2>
      )}
      <p className="dim settings-intro">
        The connection is read-only: it never marks mail read, never deletes, and never
        sends. Approved replies are only ever saved as drafts.
      </p>
      <p className="settings-warning">
        Use an <strong>app-specific IMAP password</strong>, do NOT give me your real account password!
        For Gmail that's Google Account → Security → 2-Step Verification → App passwords; most providers have an equivalent. 
      </p>

      <div className="settings-form">
        <md-outlined-select
          label="Provider"
          value={provider}
          disabled={!loaded || busy}
          onInput={(e) => handleProvider((e.currentTarget as MdSelectElement).value)}
        >
          {PROVIDERS.map((p) => (
            <md-select-option key={p.id} value={p.id} selected={p.id === provider}>
              <div slot="headline">{p.label}</div>
            </md-select-option>
          ))}
        </md-outlined-select>

        <md-outlined-text-field
          label="IMAP host"
          value={host}
          disabled={!loaded || busy || provider !== 'custom'}
          supporting-text={provider !== 'custom' ? 'prefilled by the provider choice' : ''}
          onInput={(e) => setHost((e.currentTarget as MdTextFieldElement).value)}
        />
        <md-outlined-text-field
          label="Username (email address)"
          value={user}
          disabled={!loaded || busy}
          onInput={(e) => {
            const next = (e.currentTarget as MdTextFieldElement).value
            setUser(next)
            onUsernameChange(next)
          }}
        />
        <md-outlined-text-field
          label="App password"
          type="password"
          value={password}
          disabled={!loaded || busy}
          placeholder={passwordSaved === 'set' ? '•••••••• (saved — leave blank to keep)' : ''}
          supporting-text={
            passwordSaved === 'set'
              ? 'a password is saved; type here only to replace it'
              : 'no password saved yet'
          }
          onInput={(e) => setPassword((e.currentTarget as MdTextFieldElement).value)}
        />
        <md-outlined-text-field
          label="Inbox folder"
          value={folder}
          disabled={!loaded || busy}
          supporting-text="mailbox to read (default INBOX)"
          onInput={(e) => setFolder((e.currentTarget as MdTextFieldElement).value)}
        />
        <md-outlined-text-field
          label="Drafts folder"
          value={draftsFolder}
          disabled={!loaded || busy}
          supporting-text="where approved replies are saved (never sent)"
          onInput={(e) =>
            setDraftsFolder((e.currentTarget as MdTextFieldElement).value)
          }
        />
        <md-outlined-text-field
          label="Days to fetch"
          type="number"
          value={days}
          min="1"
          max="365"
          disabled={!loaded || busy}
          supporting-text="unread mail from the last 7 days by default"
          onInput={(e) => setDays((e.currentTarget as MdTextFieldElement).value)}
        />

        <div className="settings-actions">
          <md-outlined-button
            type="button"
            disabled={!loaded || busy}
            onClick={handleReset}
          >
            Reset IMAP
          </md-outlined-button>
          <md-outlined-button type="button" disabled={!loaded || busy} onClick={handleTest}>
            Test connection
          </md-outlined-button>
          <md-filled-button type="button" disabled={!loaded || busy} onClick={handleSave}>
            Save
          </md-filled-button>
          <md-filled-button
            type="button"
            disabled={!loaded || busy}
            onClick={handleSaveAndProcess}
          >
            Save &amp; process mail
          </md-filled-button>
        </div>

        {status.kind !== 'idle' && (
          <div className={`settings-status settings-status-${status.kind}`} role="status">
            {status.message}
          </div>
        )}
      </div>
    </section>
  )
}

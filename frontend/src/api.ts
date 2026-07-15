// Typed client for the local review API. All requests go through the Vite
// dev proxy, which injects the per-run session token server-side — no token
// (and no de-anonymization mapping) ever exists in browser JavaScript.
// Everything in QueueRecordDTO is attacker-controlled email content and must
// only ever be rendered as text.

export interface EmailDTO {
  id: string
  from_addr: string
  to_addrs: string[]
  subject: string
  date: string
  body_plain: string
  thread_id: string | null
}

export interface ResultDTO {
  category: string
  confidence: number
  summary: string
  action_items: string[]
  reasoning: string
}

export interface DecisionDTO {
  escalate: boolean
  reason: string
  score: number
}

export interface QueueRecordDTO {
  email: EmailDTO
  result: ResultDTO
  decision: DecisionDTO
  draft: string | null
  provenance: string
  claude_used: boolean
  error: string | null
  importance: number
  importance_reason: string
  ranked_by: string
  source: string
  placeholder_count: number
}

export type ReviewAction = 'approve' | 'edit' | 'reject'

export interface ReviewResponse {
  ok: boolean
  action: ReviewAction
  saved_path: string | null
  note: string | null
  warning: string | null
}

export interface ImapSettingsDTO {
  host: string
  user: string
  folder: string
  password: 'set' | 'unset'
}

export interface ImapTestResponse {
  ok: boolean
  message?: string
  error?: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  let data: unknown = null
  try {
    data = await res.json()
  } catch {
    // Non-JSON response (e.g. proxy failure) — fall through to status check.
  }
  if (!res.ok) {
    const message =
      data && typeof data === 'object' && 'error' in data
        ? String((data as { error: unknown }).error)
        : `${res.status} ${res.statusText}`
    throw new Error(message)
  }
  return data as T
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function fetchQueue(): Promise<QueueRecordDTO[]> {
  return request<QueueRecordDTO[]>('/api/queue')
}

export function postReview(
  emailId: string,
  action: ReviewAction,
  draft: string,
): Promise<ReviewResponse> {
  return post<ReviewResponse>('/api/review', { email_id: emailId, action, draft })
}

export function openInbox(): Promise<{ ok: boolean; path: string }> {
  return post<{ ok: boolean; path: string }>('/api/open-inbox', {})
}

export function fetchImapSettings(): Promise<ImapSettingsDTO> {
  return request<ImapSettingsDTO>('/api/settings/imap')
}

export interface ImapSettingsForm {
  host: string
  user: string
  password: string // '' = keep the saved one
  folder: string
}

export function saveImapSettings(
  form: ImapSettingsForm,
): Promise<{ ok: boolean; password: 'set' | 'unset' }> {
  return post<{ ok: boolean; password: 'set' | 'unset' }>('/api/settings/imap', form)
}

export function testImapSettings(form: ImapSettingsForm): Promise<ImapTestResponse> {
  return post<ImapTestResponse>('/api/settings/imap/test', form)
}

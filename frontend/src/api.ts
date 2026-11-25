export type StepKey = 'discover' | 'plan' | 'design'
export type StepStatus = 'locked' | 'open' | 'awaiting_approval' | 'approved'

export interface User { id: number; name: string; role: string }
export interface Project { id: number; name: string; created_at: string }
export interface StepSpace { id: number; key: StepKey; order: number; status: StepStatus; author_id: number | null; rejection_reason: string }
export interface ChatSession { id: number; project_id: number; step: StepKey; title: string }
export interface Attachment { file_id: number; filename: string; duplicate: boolean }
export interface ChatMessage {
  id: number; role: 'user' | 'assistant'; kind: 'text' | 'confirmation' | 'deliverable' | 'note'
  content: string; attachments_json: string; actions_json: string; artifact_id: number | null
}
export interface SessionFile { id: number; filename: string; size_bytes: number; readable: boolean; flagged_injection: boolean; note: string }
export interface Artifact { id: number; artifact_id: string; title: string; status: string; version: number }
export interface SessionDetail {
  session: ChatSession; step_status: StepStatus; messages: ChatMessage[]; files: SessionFile[]; artifacts: Artifact[]
}
export type RunEvent =
  | { type: 'delta'; text: string }
  | { type: 'done'; message: ChatMessage; artifact: Artifact | null }
  | { type: 'error'; message: string }

export interface SkillInfo {
  key: string; name: string; pack: string; description: string; group: 'standalone' | 'advanced' | 'phase' | 'platform'
  runnable: boolean; needs: string; sha256: string
}
export interface SkillCatalogue {
  skills: SkillInfo[]; counts: Record<string, number>; warning: string | null
  suggestions: Record<string, { skill: string; name: string; reason: string }[]>
}
export interface DiscoverySkill {
  key: string; name: string; description: string; note: string; produces: string[]
  source: 'core' | 'optional' | 'custom'; available: boolean; needs: string; selected: boolean; order: number | null
}
export interface RunArtifact { id: number; artifact_id: string; version: number; doc_type: string; status: string }
export interface DiscoveryRun {
  id: number; number: number; session_id: number; status: 'running' | 'completed' | 'failed'
  approval: 'pending' | 'approved' | 'rejected' | 'superseded' | 'n/a'
  skills: string[]; current_skill: string; error: string; rejection_reason: string
  started_by: number; started_by_name: string; approved_by_name: string; started_at: string; ended_at: string | null
  artifacts: RunArtifact[]; live: { skill: string; text: string } | null
  handoff: Handoff | null
}
export interface Handoff {
  status: 'pending' | 'sent' | 'failed' | 'not_configured'; jira_key: string; jira_url: string; error: string; attempts: number
  created_issue: boolean; attached: boolean; assigned: boolean
}
export interface TicketPreview { summary: string; description: string; labels: string[]; attachments: string[]; jira_configured: boolean }
export type DiscoveryStatus = 'not_started' | 'running' | 'failed' | 'awaiting_approval' | 'approved' | 'rejected' | 'sent_to_etl_crew'
export interface DiscoveryState { status: DiscoveryStatus; skills: DiscoverySkill[]; selected: string[]; runs: DiscoveryRun[] }

export type RunRequest = { mode: 'chat' | 'start' } | { mode: 'skill'; skill: string; instruction: string }

let actingUserId: number | null = null
export const setActingUser = (id: number) => { actingUserId = id }

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra)
  if (actingUserId !== null) h.set('X-User-Id', String(actingUserId))
  return h
}

async function failure(res: Response): Promise<Error> {
  const body = await res.json().catch(() => ({}))
  return new Error(typeof body.detail === 'string' ? body.detail : `${res.status} ${res.statusText}`)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { ...init, headers: headers(init.headers) })
  if (!res.ok) throw await failure(res)
  return res.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export const api = {
  users: () => request<User[]>('/users'),
  config: () => request<{ runner: string; model: string | null }>('/config'),
  skills: () => request<SkillCatalogue>('/skills'),
  projects: () => request<Project[]>('/projects'),
  createProject: (name: string) => request<Project>('/projects', json({ name })),
  project: (id: number) => request<{ project: Project; steps: StepSpace[] }>(`/projects/${id}`),
  sessions: (projectId: number) => request<ChatSession[]>(`/projects/${projectId}/sessions`),
  createSession: (projectId: number, step: StepKey, title: string) =>
    request<SessionDetail>(`/projects/${projectId}/sessions`, json({ step, title })),
  session: (id: number) => request<SessionDetail>(`/sessions/${id}`),
  postMessage: (id: number, text: string, files: File[]) => {
    const form = new FormData()
    form.append('text', text)
    files.forEach((f) => form.append('files', f))
    return request<{ messages: ChatMessage[]; needs_reply: boolean }>(`/sessions/${id}/messages`, { method: 'POST', body: form })
  },
  postPaths: (id: number, paths: string[]) =>
    request<{ messages: ChatMessage[]; needs_reply: boolean }>(`/sessions/${id}/messages/paths`, json({ paths, text: '' })),
  decline: (id: number) => request<{ messages: ChatMessage[] }>(`/sessions/${id}/decline`, { method: 'POST' }),
  gate: (projectId: number, step: StepKey, action: 'submit' | 'approve') =>
    request<StepSpace>(`/projects/${projectId}/steps/${step}/${action}`, { method: 'POST' }),
  reject: (projectId: number, step: StepKey, reason: string) =>
    request<StepSpace>(`/projects/${projectId}/steps/${step}/reject`, json({ reason })),
  downloadUrl: (artifactId: number) => `/api/artifacts/${artifactId}/download`,
  artifact: (id: number) => request<{ artifact: { artifact_id: string; version: number; status: string }; content: string }>(`/artifacts/${id}`),
  discovery: (projectId: number) => request<DiscoveryState>(`/projects/${projectId}/discovery`),
  setDiscoverySkills: (projectId: number, skills: string[]) =>
    request<{ selected: string[] }>(`/projects/${projectId}/discovery/skills`, { ...json({ skills }), method: 'PUT' }),
  uploadSkill: (projectId: number, file: File, replace: boolean) => {
    const form = new FormData()
    form.append('file', file)
    form.append('project_id', String(projectId))
    form.append('replace', String(replace))
    return request<{ key: string; name: string }>('/skills/upload', { method: 'POST', body: form })
  },
  startDiscovery: (sessionId: number) => request<DiscoveryRun>(`/sessions/${sessionId}/discovery`, { method: 'POST' }),
  run: (id: number) => request<DiscoveryRun>(`/runs/${id}`),
  approveRun: (id: number) => request<DiscoveryRun>(`/runs/${id}/approve`, { method: 'POST' }),
  rejectRun: (id: number, reason: string) => request<DiscoveryRun>(`/runs/${id}/reject`, json({ reason })),
  ticketPreview: (id: number) => request<TicketPreview>(`/runs/${id}/handoff/preview`),
  retryHandoff: (id: number) => request<DiscoveryRun>(`/runs/${id}/handoff/retry`, { method: 'POST' }),
}

/** POST /run and read the Server-Sent Events stream. Resolves when the stream ends. */
export async function runSession(id: number, request: RunRequest, onEvent: (e: RunEvent) => void): Promise<void> {
  const res = await fetch(`/api/sessions/${id}/run`, { ...json(request), headers: headers({ 'Content-Type': 'application/json' }) })
  if (!res.ok || !res.body) throw await failure(res)
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let end: number
    while ((end = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      if (frame.startsWith('data: ')) onEvent(JSON.parse(frame.slice(6)) as RunEvent)
    }
  }
}

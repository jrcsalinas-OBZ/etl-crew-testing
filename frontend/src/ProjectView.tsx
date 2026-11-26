import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { ChatSession, DiscoveryState, DiscoveryStatus, Project, User } from './api'
import { ChatPane } from './ChatPane'
import { DiscoverySkills } from './DiscoverySkills'
import { RunReview } from './RunReview'
import { confirmAndDeleteProject } from './deleteProject'

export const STATUS_LABEL: Record<DiscoveryStatus, string> = {
  not_started: 'Not started', running: 'Running', failed: 'Last run failed', awaiting_approval: 'Awaiting approval',
  approved: 'Approved — Jira ticket pending', rejected: 'Rejected — run again', sent_to_etl_crew: 'Sent to ETL Crew',
}
const RUN_LABEL: Record<string, string> = {
  pending: 'awaiting approval', approved: 'approved', rejected: 'rejected', superseded: 'superseded', 'n/a': 'failed',
}

type View = { kind: 'session'; id: number } | { kind: 'run'; id: number } | null

export function ProjectView({ projectId, actingUser, onDeleted }: {
  projectId: number; actingUser: User | undefined; onDeleted: () => void
}) {
  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [state, setState] = useState<DiscoveryState | null>(null)
  const [view, setView] = useState<View>(null)
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [detail, list, disc] = await Promise.all([api.project(projectId), api.sessions(projectId), api.discovery(projectId)])
      setProject(detail.project); setSessions(list); setState(disc)
      const latest = list[list.length - 1]
      setView((current) => current ?? (latest ? { kind: 'session', id: latest.id } : null))  // open the latest session
    } catch (e) { setError((e as Error).message) }
  }, [projectId])
  useEffect(() => { load() }, [load])

  // Keep the sidebar status fresh while a run is going
  const running = state?.runs.some((r) => r.status === 'running')
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => { api.discovery(projectId).then(setState).catch(() => undefined) }, 2500)
    return () => clearInterval(timer)
  }, [running, projectId])

  const newSession = async () => {
    setError(null)
    try {
      const created = await api.createSession(projectId, 'discover', '')
      setView({ kind: 'session', id: created.session.id })
      await load()
    } catch (e) { setError((e as Error).message) }
  }

  const remove = async () => {
    if (!project) return
    setError(null)
    try { if (await confirmAndDeleteProject(project)) onDeleted() } catch (e) { setError((e as Error).message) }
  }

  if (!project || !state) return <p className="hint">{error ?? 'Loading…'}</p>

  return (
    <div className="project">
      <nav className="rail" aria-label="Project">
        <h2>{project.name}</h2>
        <div className="stepbox">
          <header><span>Discovery</span><small className={`status s-${state.status}`}>{STATUS_LABEL[state.status]}</small></header>
          <small className="hint">Skills: {state.selected.map((k) => state.skills.find((s) => s.key === k)?.name ?? k).join(' → ')}</small>
          <button className="link" onClick={() => setSkillsOpen(true)}>Change discovery skills…</button>
        </div>

        <div className="stepbox">
          <header><span>Sessions</span></header>
          <ul className="sessions">
            {sessions.map((s) => (
              <li key={s.id}>
                <button className={view?.kind === 'session' && view.id === s.id ? 'session active' : 'session'}
                  onClick={() => setView({ kind: 'session', id: s.id })}>💬 {s.title}</button>
              </li>
            ))}
          </ul>
          <button className="link" onClick={newSession}>+ New session</button>
        </div>

        <div className="stepbox">
          <header><span>Runs</span></header>
          {state.runs.length === 0 && <small className="hint">No discovery runs yet.</small>}
          <ul className="sessions">
            {state.runs.map((r) => (
              <li key={r.id}>
                <button className={view?.kind === 'run' && view.id === r.id ? 'session active' : 'session'}
                  onClick={() => setView({ kind: 'run', id: r.id })}>
                  🔎 Discovery #{r.number} <small className={`status r-${r.status === 'completed' ? r.approval : r.status}`}>
                    {r.status === 'running' ? 'running…' : RUN_LABEL[r.approval]}</small>
                </button>
              </li>
            ))}
          </ul>
        </div>
        <button className="link danger" onClick={remove}
          disabled={state.status === 'running'} title={state.status === 'running' ? 'Wait for the discovery run to finish' : ''}>
          🗑 Delete project
        </button>
      </nav>

      <section className="content">
        {error && <p className="banner error" role="alert">{error}</p>}
        {view === null && (
          <div className="empty">
            <h3>Start a session to upload documents</h3>
            <p className="hint">Upload design documents, job inventories and other source material, then run discovery.</p>
            <button className="primary big" onClick={newSession}>Start a session</button>
          </div>
        )}
        {view?.kind === 'session' && (
          <ChatPane key={`s${view.id}`} sessionId={view.id} discovery={state} onChanged={load}
            onOpenSkills={() => setSkillsOpen(true)} onOpenRun={(id) => setView({ kind: 'run', id })} />
        )}
        {view?.kind === 'run' && (
          <RunReview key={`r${view.id}`} runId={view.id} actingUser={actingUser} onChanged={load}
            onOpenSession={(id) => setView({ kind: 'session', id })} />
        )}
      </section>

      {skillsOpen && (
        <DiscoverySkills projectId={projectId} skills={state.skills} selected={state.selected}
          onSaved={load} onClose={() => setSkillsOpen(false)} />
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { api, setActingUser } from './api'
import type { User } from './api'
import { ProjectList } from './ProjectList'
import { ProjectView } from './ProjectView'
import './App.css'

export default function App() {
  const [users, setUsers] = useState<User[]>([])
  const [actingId, setActingId] = useState<number | null>(null)
  const [projectId, setProjectId] = useState<number | null>(null)
  const [runner, setRunner] = useState<{ runner: string; model: string | null } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.users().then((list) => {
      setUsers(list)
      const author = list.find((u) => u.role === 'author') ?? list[0]
      if (author) { setActingUser(author.id); setActingId(author.id) }
    }).catch((e: Error) => setError(`Cannot reach the API: ${e.message}`))
    const refreshConfig = () => api.config().then(setRunner).catch(() => undefined)
    refreshConfig()
    window.addEventListener('focus', refreshConfig)  // pick up a newly saved API key without a reload
    return () => window.removeEventListener('focus', refreshConfig)
  }, [])

  const switchUser = (id: number) => { setActingUser(id); setActingId(id) }

  return (
    <div className="app">
      <header className="topbar">
        <button className="brand" onClick={() => setProjectId(null)}>Trinity Workbench</button>
        <div className="topright">
          {runner && (runner.runner === 'api'
            ? <span className="chip ok" title="Responses come from the Claude API">Claude API · {runner.model}</span>
            : runner.runner === 'cli'
              ? <span className="chip ok" title="Responses come from Claude through your logged-in Claude Code account">Claude · your account</span>
              : <span className="chip warn" title="Log in with `claude` (then /login), or add ANTHROPIC_API_KEY to .env. No restart needed.">Stub mode — not connected to Claude</span>)}
          <label className="acting">
            Acting as
            <select value={actingId ?? ''} onChange={(e) => switchUser(Number(e.target.value))}>
              {users.map((u) => <option key={u.id} value={u.id}>{u.name} ({u.role})</option>)}
            </select>
          </label>
        </div>
      </header>
      {error && <p className="banner error" role="alert">{error}</p>}
      <main>
        {projectId === null
          ? <ProjectList onOpen={setProjectId} />
          : <ProjectView key={projectId} projectId={projectId} actingUser={users.find((u) => u.id === actingId)}
              onDeleted={() => setProjectId(null)} />}
      </main>
    </div>
  )
}

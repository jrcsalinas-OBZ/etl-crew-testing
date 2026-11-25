import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from './api'
import type { Project } from './api'

const created = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

export function ProjectList({ onOpen }: { onOpen: (id: number) => void }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { api.projects().then(setProjects).catch((e: Error) => setError(e.message)) }, [])

  const create = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      const project = await api.createProject(name)
      setName('')
      onOpen(project.id)
    } catch (err) { setError((err as Error).message) }
  }

  return (
    <div>
      <h2>Projects</h2>
      <p className="hint">Each project keeps its own files, sessions and results, so work on one never mixes with another.</p>
      {error && <p className="banner error" role="alert">{error}</p>}
      <form className="new-project" onSubmit={create}>
        <input required autoFocus placeholder="New project name" aria-label="New project name" value={name}
          onChange={(e) => setName(e.target.value)} />
        <button type="submit">Create project</button>
      </form>
      {projects.length === 0 && <p className="hint">No projects yet. Name one above to get started.</p>}
      <ul className="cards">
        {projects.map((p) => (
          <li key={p.id}>
            <button className="card" onClick={() => onOpen(p.id)}>
              <strong>{p.name}</strong>
              <small>Created {created(p.created_at)}</small>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

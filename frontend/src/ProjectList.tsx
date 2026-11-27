import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from './api'
import type { Project } from './api'
import { confirmAndDeleteProject } from './deleteProject'

const created = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
const monogram = (name: string) => [...name.trim()][0]?.toUpperCase() ?? '#'

export function ProjectList({ onOpen }: { onOpen: (id: number) => void }) {
  const [projects, setProjects] = useState<Project[] | null>(null)  // null = not loaded (yet, or the load failed)
  const [loadFailed, setLoadFailed] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.projects().then(setProjects).catch((e: Error) => { setError(e.message); setLoadFailed(true) })
  }, [])

  const remove = async (project: Project) => {
    setError(null)
    try {
      if (await confirmAndDeleteProject(project)) setProjects((list) => (list ?? []).filter((p) => p.id !== project.id))
    } catch (err) { setError((err as Error).message) }
  }

  const create = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      const project = await api.createProject(name)
      setName('')
      onOpen(project.id)
    } catch (err) { setError((err as Error).message) }
  }

  const loading = projects === null && !loadFailed

  return (
    <div className="home">
      <section className="home-hero" aria-labelledby="home-title">
        <p className="eyebrow">Trinity Workbench</p>
        <h1 id="home-title">Projects</h1>
        <p className="lead">Each project keeps its own files, sessions and results, so work on one never mixes with another.</p>
        <form className="new-project" onSubmit={create}>
          <label htmlFor="new-project-name">New project name</label>
          <div className="new-project-row">
            <input id="new-project-name" required autoFocus placeholder="e.g. Finance warehouse migration" value={name}
              onChange={(e) => setName(e.target.value)} />
            <button type="submit">Create project</button>
          </div>
        </form>
      </section>

      {error && <p className="banner error" role="alert">{error}</p>}

      <section className="home-list" aria-labelledby="list-title">
        <header className="home-list-head">
          <h2 id="list-title">Your projects</h2>
          {projects && projects.length > 0 && <span className="count" aria-label={`${projects.length} projects`}>{projects.length}</span>}
        </header>

        {loading && (
          <>
            <p className="sr-only" role="status">Loading projects…</p>
            <ul className="cards" aria-hidden="true">
              {[0, 1, 2].map((i) => <li key={i} className="card-skeleton" />)}
            </ul>
          </>
        )}

        {projects?.length === 0 && (
          <div className="home-empty">
            <h3>Start your first project</h3>
            <p className="hint">Name it above. Once it exists you can upload source documents and run discovery.</p>
          </div>
        )}

        {projects && projects.length > 0 && (
          <ul className="cards">
            {projects.map((p) => (
              <li key={p.id} className="card-wrap">
                <button className="card" onClick={() => onOpen(p.id)}>
                  <span className="monogram" aria-hidden="true">{monogram(p.name)}</span>
                  <strong>{p.name}</strong>
                  <small>Created {created(p.created_at)}</small>
                </button>
                <button className="card-delete" title={`Delete ${p.name}`} aria-label={`Delete ${p.name}`}
                  onClick={() => remove(p)}>Delete</button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

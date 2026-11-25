import { useState } from 'react'
import type { ChangeEvent } from 'react'
import { api } from './api'
import type { DiscoverySkill } from './api'

interface Props {
  projectId: number
  skills: DiscoverySkill[]
  selected: string[]
  onSaved: () => void
  onClose: () => void
}

const SOURCE_LABEL = { core: 'Trinity default', optional: 'Optional', custom: 'Your skill' }

export function DiscoverySkills({ projectId, skills, selected, onSaved, onClose }: Props) {
  const [chain, setChain] = useState<string[]>(selected)
  const [replace, setReplace] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const byKey = new Map(skills.map((s) => [s.key, s]))
  const unselected = skills.filter((s) => !chain.includes(s.key))

  const move = (index: number, delta: number) => setChain((c) => {
    const next = [...c]
    const [item] = next.splice(index, 1)
    next.splice(index + delta, 0, item)
    return next
  })

  const save = async () => {
    setBusy(true); setNotice(null)
    try { await api.setDiscoverySkills(projectId, chain); onSaved(); onClose() }
    catch (e) { setNotice({ kind: 'error', text: (e as Error).message }) }
    finally { setBusy(false) }
  }

  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setBusy(true); setNotice(null)
    try {
      const added = await api.uploadSkill(projectId, file, replace)
      setNotice({ kind: 'ok', text: `Added “${added.name}”. Tick it below to use it in discovery.` })
      onSaved()
    } catch (err) { setNotice({ kind: 'error', text: (err as Error).message }) }
    finally { setBusy(false) }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Discovery skills" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header><h3>Discovery skills</h3><button onClick={onClose} aria-label="Close">✕</button></header>
        <p className="hint">These run in order, top to bottom. Each skill sees your documents plus what the skills above it produced.</p>

        <h4>Will run</h4>
        {chain.length === 0 && <p className="banner warn">Pick at least one skill.</p>}
        <ol className="chain">
          {chain.map((key, i) => {
            const s = byKey.get(key)
            return (
              <li key={key}>
                <div>
                  <strong>{s?.name ?? key}</strong> <span className="chip">{s ? SOURCE_LABEL[s.source] : ''}</span>
                  <small> → {s?.produces.join(', ')}</small>
                  {s?.note && <div className="hint">{s.note}</div>}
                </div>
                <span className="row-actions">
                  <button disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${s?.name} up`}>↑</button>
                  <button disabled={i === chain.length - 1} onClick={() => move(i, 1)} aria-label={`Move ${s?.name} down`}>↓</button>
                  <button onClick={() => setChain((c) => c.filter((k) => k !== key))} aria-label={`Remove ${s?.name}`}>Remove</button>
                </span>
              </li>
            )
          })}
        </ol>

        <h4>Available</h4>
        <ul className="chain">
          {unselected.length === 0 && <li className="hint">Every available skill is already selected.</li>}
          {unselected.map((s) => (
            <li key={s.key} className={s.available ? '' : 'muted'}>
              <div>
                <strong>{s.name}</strong> <span className="chip">{SOURCE_LABEL[s.source]}</span>
                <small> → {s.produces.join(', ')}</small>
                <div className="hint">{s.available ? (s.note || s.description) : 'Not installed — see skills/README.md'}</div>
              </div>
              <button disabled={!s.available} onClick={() => setChain((c) => [...c, s.key])}>Add</button>
            </li>
          ))}
        </ul>

        <div className="add-skill">
          <label className={`attach ${busy ? 'disabled' : ''}`}>
            <input type="file" accept=".md,.zip" disabled={busy} onChange={upload} aria-label="Add a skill" />
            ＋ Add a skill (SKILL.md or .zip)
          </label>
          <label className="check"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Replace if it exists</label>
        </div>
        {notice && <p className={`banner ${notice.kind}`} role="status">{notice.text}</p>}

        <footer className="modal-actions">
          <button onClick={() => setChain(skills.filter((s) => s.source === 'core' && s.available).map((s) => s.key))}>Reset to Trinity default</button>
          <button className="primary" disabled={busy || chain.length === 0} onClick={save}>Save</button>
        </footer>
      </div>
    </div>
  )
}

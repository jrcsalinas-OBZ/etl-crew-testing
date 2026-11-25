import { useState } from 'react'
import type { SkillCatalogue, SkillInfo } from './api'

type Tab = 'standalone' | 'advanced' | 'phase'
const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'standalone', label: 'Run on your documents', hint: 'Work on whatever you upload. Start here.' },
  { id: 'advanced', label: 'Needs earlier records', hint: 'Expect records from cutover work (runbooks, cutover logs). Upload them as documents.' },
  { id: 'phase', label: 'Phase chain', hint: 'Each phase expects the previous phase’s approved output. Not recommended one by one.' },
]

interface Props {
  catalogue: SkillCatalogue
  onRun: (skill: SkillInfo, instruction: string) => void
  onClose: () => void
}

export function SkillPicker({ catalogue, onRun, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('standalone')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<SkillInfo | null>(null)
  const [instruction, setInstruction] = useState('')

  const shown = catalogue.skills.filter((s) => s.group === tab || (tab === 'standalone' && s.pack !== 'data-migration' && s.group !== 'platform'))
    .filter((s) => !query || `${s.name} ${s.description}`.toLowerCase().includes(query.toLowerCase()))

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Run a skill" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header>
          <h3>Run a skill</h3>
          <button onClick={onClose} aria-label="Close">✕</button>
        </header>
        <p className="hint">Skills run one at a time, each on this session’s documents. Missing inputs become stated assumptions — nothing blocks.</p>
        {catalogue.warning && <p className="banner warn">{catalogue.warning}</p>}
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? 'tab active' : 'tab'}
              onClick={() => { setTab(t.id); setSelected(null) }}>{t.label}</button>
          ))}
        </div>
        <p className="hint">{TABS.find((t) => t.id === tab)?.hint}</p>
        <input className="search" placeholder="Search skills…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="skill-body">
          <ul className="skill-list">
            {shown.length === 0 && <li className="hint">No skills here.</li>}
            {shown.map((s) => (
              <li key={s.key}>
                <button className={selected?.key === s.key ? 'skill-row active' : 'skill-row'} onClick={() => setSelected(s)}>
                  <strong>{s.name}</strong>
                  <small>{s.description.length > 140 ? `${s.description.slice(0, 140)}…` : s.description}</small>
                </button>
              </li>
            ))}
          </ul>
          <aside className="skill-detail">
            {selected ? (
              <>
                <h4>{selected.name} <span className="chip">{selected.pack}</span></h4>
                <p>{selected.description}</p>
                {selected.needs && <p className="hint"><strong>Works best with:</strong> {selected.needs}</p>}
                <label>Instruction (optional)
                  <textarea rows={3} value={instruction} onChange={(e) => setInstruction(e.target.value)}
                    placeholder="e.g. Focus on the hourly feeds, or paste a change request…" />
                </label>
                <button className="primary" onClick={() => onRun(selected, instruction)}>Run {selected.name}</button>
              </>
            ) : <p className="hint">Pick a skill to see what it does.</p>}
          </aside>
        </div>
      </div>
    </div>
  )
}

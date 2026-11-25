import { useCallback, useEffect, useRef, useState } from 'react'
import type { ClipboardEvent, DragEvent, KeyboardEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, runSession } from './api'
import type { Attachment, ChatMessage, DiscoveryRun, DiscoveryState, RunRequest, SessionDetail, SkillCatalogue, SkillInfo } from './api'
import { SkillPicker } from './SkillPicker'

const ACCEPT = '.pdf,.docx,.xlsx,.xlsm,.csv,.tsv,.txt,.md,.sql,.hql,.json,.yml,.yaml,.ipynb,.py,.xml'

interface Props {
  sessionId: number
  discovery: DiscoveryState
  onChanged: () => void
  onOpenSkills: () => void
  onOpenRun: (runId: number) => void
}

export function ChatPane({ sessionId, discovery, onChanged, onOpenSkills, onOpenRun }: Props) {
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [catalogue, setCatalogue] = useState<SkillCatalogue | null>(null)
  const [picker, setPicker] = useState(false)
  const [streaming, setStreaming] = useState<string | null>(null)
  const [liveRun, setLiveRun] = useState<DiscoveryRun | null>(null)
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [pathMode, setPathMode] = useState(false)
  const [pathText, setPathText] = useState('')
  const bottom = useRef<HTMLDivElement>(null)

  const reload = useCallback(async () => {
    try { setDetail(await api.session(sessionId)) } catch (e) { setError((e as Error).message) }
  }, [sessionId])

  // the parent keys this component by session id, so state is fresh per session
  useEffect(() => { reload() }, [reload])
  useEffect(() => { api.skills().then(setCatalogue).catch(() => undefined) }, [])

  // Follow a discovery run started from this session: poll its live output until it finishes
  const runningHere = discovery.runs.find((r) => r.status === 'running' && r.session_id === sessionId)
  const followId = liveRun?.status === 'running' ? liveRun.id : runningHere?.id
  useEffect(() => {
    if (followId === undefined) return
    let stop = false
    const poll = async () => {
      try {
        const run = await api.run(followId)
        if (stop) return
        setLiveRun(run)
        if (run.status === 'running') setTimeout(poll, 1500)
        else { await reload(); onChanged() }
      } catch { if (!stop) setTimeout(poll, 3000) }
    }
    poll()
    return () => { stop = true }
  }, [followId, reload, onChanged])

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) },
    [detail?.messages.length, streaming, liveRun?.live?.text.length])

  const append = (messages: ChatMessage[]) =>
    setDetail((d) => (d ? { ...d, messages: [...d.messages, ...messages] } : d))

  const stream = async (request: RunRequest) => {
    setStreaming('')
    try {
      await runSession(sessionId, request, (event) => {
        if (event.type === 'delta') setStreaming((s) => (s ?? '') + event.text)
        else if (event.type === 'error') setError(event.message)
      })
    } catch (e) { setError((e as Error).message) }
    setStreaming(null)
    await reload()
    onChanged()
  }

  const guard = async (work: () => Promise<void>) => {
    setBusy(true); setError(null)
    try { await work() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const send = () => guard(async () => {
    const message = text.trim()
    if (!message) return
    setText('')
    const res = await api.postMessage(sessionId, message, [])
    append(res.messages)
    if (res.needs_reply) await stream({ mode: 'chat' })
  })

  const upload = (files: File[]) => guard(async () => {
    if (files.length === 0) return
    const res = await api.postMessage(sessionId, text.trim(), files)
    setText('')
    append(res.messages)
    await reload()
  })

  const addPaths = () => guard(async () => {
    const paths = pathText.split('\n').map((p) => p.trim()).filter(Boolean)
    if (paths.length === 0) return
    const res = await api.postPaths(sessionId, paths)
    setPathText(''); setPathMode(false)
    append(res.messages)
    await reload()
  })

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files)
    if (files.length > 0) { e.preventDefault(); upload(files) }  // files copied in Explorer / screenshots
  }

  const runDiscovery = () => guard(async () => {
    const run = await api.startDiscovery(sessionId)
    setLiveRun(run)
    await reload()
    onChanged()
  })
  const runSkill = (key: string, instruction = '') => { setPicker(false); return guard(() => stream({ mode: 'skill', skill: key, instruction })) }
  const later = () => guard(async () => { append((await api.decline(sessionId)).messages) })

  const onDrop = (e: DragEvent) => { e.preventDefault(); setDragging(false); upload(Array.from(e.dataTransfer.files)) }
  const onKey = (e: KeyboardEvent) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }

  if (!detail) return <p className="hint">{error ?? 'Loading session…'}</p>

  const readOnly = detail.step_status !== 'open'
  const discoveryRunning = discovery.runs.some((r) => r.status === 'running') || liveRun?.status === 'running'
  const working = busy || streaming !== null
  const canRun = !readOnly && !working && !discoveryRunning && detail.files.some((f) => f.readable)
  const lastId = detail.messages[detail.messages.length - 1]?.id
  const chain = discovery.selected.map((k) => discovery.skills.find((s) => s.key === k)?.name ?? k).join(' → ')

  return (
    <div className={`chat ${dragging ? 'dragging' : ''}`}
      onDragOver={(e) => { e.preventDefault(); if (!readOnly) setDragging(true) }}
      onDragLeave={() => setDragging(false)} onDrop={readOnly ? undefined : onDrop}>
      <div className="chat-head">
        <strong>{detail.session.title}</strong>
        <span className="hint">{detail.files.length} file{detail.files.length === 1 ? '' : 's'} in this session’s context</span>
        <button disabled={readOnly || working || !catalogue} onClick={() => setPicker(true)}
          title="Run a single skill on its own, outside discovery">Run one skill…</button>
        <button className="primary" disabled={!canRun} onClick={runDiscovery}
          title={discoveryRunning ? 'A discovery run is already in progress' : `Runs: ${chain}`}>Run discovery</button>
      </div>

      {detail.files.length > 0 && (
        <ul className="filechips" aria-label="Files in this session">
          {detail.files.map((f) => (
            <li key={f.id} className={f.readable ? '' : 'bad'} title={f.note}>
              {f.readable ? '📄' : '❌'} {f.filename}{f.flagged_injection ? ' ⚠️' : ''}
            </li>
          ))}
        </ul>
      )}

      <div className="messages" aria-live="polite">
        {detail.messages.map((m) => (
          <Bubble key={m.id} message={m} detail={detail} catalogue={catalogue}
            showActions={m.id === lastId && !readOnly && !working && !discoveryRunning} chain={chain}
            onDiscovery={runDiscovery} onSkills={onOpenSkills} onSkill={runSkill} onBrowse={() => setPicker(true)} onLater={later} />
        ))}
        {liveRun && liveRun.session_id === sessionId && liveRun.status === 'running' && (
          <div className="bubble assistant live">
            <p className="live-head">
              ⏳ Discovery #{liveRun.number} — running <strong>{liveRun.current_skill || liveRun.skills[0]}</strong>{' '}
              ({Math.max(1, liveRun.skills.indexOf(liveRun.current_skill) + 1)} of {liveRun.skills.length}).
              It keeps running if you close this tab.
            </p>
            {liveRun.live?.text ? <Markdown remarkPlugins={[remarkGfm]}>{liveRun.live.text}</Markdown> : <p className="hint">Starting…</p>}
          </div>
        )}
        {liveRun && liveRun.session_id === sessionId && liveRun.status !== 'running' && (
          <p className="run-done">
            <button className="primary" onClick={() => onOpenRun(liveRun.id)}>
              {liveRun.status === 'failed' ? `See what failed in discovery #${liveRun.number}` : `Review discovery #${liveRun.number}`}
            </button>
          </p>
        )}
        {streaming !== null && (
          <div className="bubble assistant"><Markdown remarkPlugins={[remarkGfm]}>{streaming || '…'}</Markdown></div>
        )}
        <div ref={bottom} />
      </div>

      {error && <p className="banner error" role="alert">{error}</p>}
      {working && <p className="hint working">Working… the composer is locked until this finishes.</p>}
      {readOnly ? (
        <p className="banner warn">This session is read-only.</p>
      ) : (
        <div className="composer">
          {/* A native <label> opens the file dialog without any script-triggered click, which some browsers block */}
          <label className={`attach ${working ? 'disabled' : ''}`} title="Attach files (or drop them anywhere in the chat)">
            <input type="file" multiple accept={ACCEPT} disabled={working} aria-label="Attach files"
              onChange={(e) => { const files = Array.from(e.target.files ?? []); e.target.value = ''; upload(files) }} />
            📎 Attach files
          </label>
          <textarea rows={2} placeholder="Message, or drop / paste design documents and job inventory here…" value={text}
            onChange={(e) => setText(e.target.value)} onKeyDown={onKey} onPaste={onPaste} disabled={working} />
          <button className="primary" disabled={!text.trim() || working} onClick={send}>Send</button>
        </div>
      )}
      {!readOnly && (
        <div className="pathbox">
          <button className="link" onClick={() => setPathMode((v) => !v)} aria-expanded={pathMode}>
            {pathMode ? 'Hide' : 'Attach button not working? Add files by path'}
          </button>
          {pathMode && (
            <>
              <textarea rows={3} value={pathText} onChange={(e) => setPathText(e.target.value)} disabled={working}
                placeholder={'Paste file or folder paths, one per line.\nIn Explorer: select the files, hold Shift, right-click, choose "Copy as path", then paste here.'} />
              <button className="primary" disabled={!pathText.trim() || working} onClick={addPaths}>Add files</button>
            </>
          )}
        </div>
      )}

      {picker && catalogue && (
        <SkillPicker catalogue={catalogue} onClose={() => setPicker(false)}
          onRun={(skill: SkillInfo, instruction: string) => runSkill(skill.key, instruction)} />
      )}
    </div>
  )
}

interface BubbleProps {
  message: ChatMessage; detail: SessionDetail; catalogue: SkillCatalogue | null; showActions: boolean; chain: string
  onDiscovery: () => void; onSkills: () => void; onSkill: (key: string) => void; onBrowse: () => void; onLater: () => void
}

function Bubble({ message, detail, catalogue, showActions, chain, onDiscovery, onSkills, onSkill, onBrowse, onLater }: BubbleProps) {
  const attachments = JSON.parse(message.attachments_json) as Attachment[]
  const actions = JSON.parse(message.actions_json) as string[]
  const artifact = detail.artifacts.find((a) => a.id === message.artifact_id)
  const skillName = (key: string) => catalogue?.skills.find((s) => s.key === key)?.name ?? key.split(':')[1]
  return (
    <div className={`bubble ${message.role} ${message.kind}`}>
      {attachments.length > 0 && (
        <ul className="attached">{attachments.map((a) => <li key={a.file_id}>📎 {a.filename}</li>)}</ul>
      )}
      <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
      {artifact && (
        <p className="artifact">
          <strong>{artifact.artifact_id}</strong> v{artifact.version} · <span className="chip">{artifact.status}</span> ·{' '}
          <a href={api.downloadUrl(artifact.id)}>Download .md</a>
        </p>
      )}
      {showActions && actions.length > 0 && (
        <div className="actions">
          {actions.includes('discovery') && <button className="primary" onClick={onDiscovery} title={chain}>Run discovery ({chain})</button>}
          {actions.includes('discovery_skills') && <button onClick={onSkills}>Change discovery skills…</button>}
          {actions.filter((a) => a.startsWith('skill:')).map((a) => {
            const key = a.slice('skill:'.length)
            return <button key={a} onClick={() => onSkill(key)}>Run {skillName(key)}</button>
          })}
          {actions.includes('skills') && <button onClick={onBrowse}>Browse skills…</button>}
          {actions.includes('later') && <button onClick={onLater}>Not yet</button>}
        </div>
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from './api'
import type { CrewTicket, DiscoveryRun, Handoff, TicketPreview, User } from './api'

function HandoffPanel({ runId, approvedBy, handoff, busy, onRetry }: {
  runId: number; approvedBy: string; handoff: Handoff | null; busy: boolean; onRetry: () => void
}) {
  const status = handoff?.status ?? 'pending'
  return (
    <div className={`handoff handoff-${status}`}>
      <p>
        Approved by {approvedBy}.{' '}
        {status === 'sent' && <>Sent to ETL Crew as{' '}
          {handoff?.jira_url ? <a href={handoff.jira_url} target="_blank" rel="noreferrer"><strong>{handoff.jira_key}</strong></a> : <strong>{handoff?.jira_key}</strong>}
          {' '}— created in To Do, files attached, assigned to the ETL Crew lead. Planning and design happen there.</>}
        {status === 'pending' && <>Creating the Jira ticket for ETL Crew…</>}
        {status === 'not_configured' && <>The Jira ticket was <strong>not</strong> created: {handoff?.error}</>}
        {status === 'failed' && <>The Jira ticket hand-off <strong>failed</strong>: {handoff?.error}</>}
      </p>
      {status === 'failed' || status === 'not_configured' ? (
        <div className="actions">
          <button className="primary" disabled={busy} onClick={onRetry}>Retry hand-off</button>
          {handoff?.jira_key && <small className="hint">Issue {handoff.jira_key} exists — a retry only does the remaining steps.</small>}
        </div>
      ) : null}
      <TicketPreviewBox runId={runId} />
    </div>
  )
}

function TicketPreviewBox({ runId }: { runId: number }) {
  const [preview, setPreview] = useState<TicketPreview | null>(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const toggle = async () => {
    if (!open && !preview) {
      try { setPreview(await api.ticketPreview(runId)) } catch (e) { setError((e as Error).message) }
    }
    setOpen((o) => !o)
  }
  return (
    <div className="ticket-preview">
      <button className="link" onClick={toggle} aria-expanded={open}>{open ? 'Hide' : 'Show'} the Jira ticket for ETL Crew</button>
      {error && <p className="banner error">{error}</p>}
      {open && preview && (
        <div className="ticket">
          {!preview.jira_configured && <p className="banner warn">Jira isn’t configured in .env yet, so approving won’t create this ticket until you add the JIRA_* settings.</p>}
          <p><b>Summary:</b> {preview.summary}</p>
          <p><b>Labels:</b> {preview.labels.join(', ')}</p>
          <p><b>Attachments:</b> {preview.attachments.join(', ')}</p>
          <pre>{preview.description}</pre>
        </div>
      )}
    </div>
  )
}

function CrewPanel({ run, onSent }: { run: DiscoveryRun; onSent: () => void }) {
  const [ticket, setTicket] = useState<CrewTicket | null>(null)
  const [show, setShow] = useState<'ticket' | 'curl' | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const dispatch = run.crew ?? ticket?.dispatch ?? null

  useEffect(() => {
    api.crewTicket(run.id).then(setTicket).catch((e: Error) => setError(e.message))
  }, [run.id, run.crew?.status])

  const send = async () => {
    if (!ticket || !window.confirm(`Send ${ticket.ticket_key} to the ETL Crew lead at ${ticket.opencode_url}?\n\n`
      + 'The crew will start building: it creates a branch and opens a pull request.')) return
    setBusy(true); setError(null)
    try { await api.sendCrewTicket(run.id); onSent() }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  const copyCurl = async () => {
    if (!ticket) return
    await navigator.clipboard.writeText(ticket.curl)
    setCopied(true); setTimeout(() => setCopied(false), 1500)
  }

  const approved = run.approval === 'approved'
  const problems = ticket ? [...ticket.gaps.map((g) => `Missing: ${g}`), ...ticket.unresolved.map((t) => `No reference value for {{${t}}}`)] : []
  return (
    <div className={`handoff crew handoff-${dispatch?.status ?? 'pending'}`}>
      <p>
        <b>ETL Crew (opencode):</b>{' '}
        {dispatch?.status === 'sent'
          ? <>sent as <strong>{dispatch.ticket_key}</strong> ({dispatch.artifact}) —{' '}
            {dispatch.session_url ? <a href={dispatch.session_url} target="_blank" rel="noreferrer">open session {dispatch.session_id}</a> : <>session {dispatch.session_id}</>}</>
          : dispatch?.status === 'failed' ? <>sending <strong>failed</strong>: {dispatch.error}</>
            : ticket ? <>ticket <strong>{ticket.ticket_key}</strong> from {ticket.artifact}{ticket.dummy && ' (no Jira issue — the crew is told to skip Jira steps)'}</>
              : 'loading the ticket…'}
      </p>
      {problems.length > 0 && <ul className="banner warn">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
      {ticket && !ticket.opencode_configured && <p className="banner warn">Set OPENCODE_URL in .env to send from here.</p>}
      {error && <p className="banner error" role="alert">{error}</p>}
      {ticket && (
        <div className="actions">
          {dispatch?.status !== 'sent' && (
            <button className="primary" disabled={busy || !approved || !ticket.ready || !ticket.opencode_configured} onClick={send}
              title={!approved ? 'Approve the discovery run first' : !ticket.ready ? 'Fix the problems listed above first' : ''}>
              {dispatch?.status === 'failed' ? 'Retry send' : 'Send to ETL Crew'}
            </button>
          )}
          <button onClick={() => setShow(show === 'ticket' ? null : 'ticket')}>{show === 'ticket' ? 'Hide' : 'Show'} ticket</button>
          <button onClick={() => setShow(show === 'curl' ? null : 'curl')}>{show === 'curl' ? 'Hide' : 'Show'} curl</button>
          <a href={api.crewPayloadUrl(run.id)}>Download {ticket.payload_filename}</a>
          {!approved && <small className="hint">Sending unlocks after approval.</small>}
        </div>
      )}
      {ticket && show === 'ticket' && <pre className="ticket">{ticket.text}</pre>}
      {ticket && show === 'curl' && (
        <div className="ticket">
          <p className="hint">Save the payload next to where you run this, then: <button className="link" onClick={copyCurl}>{copied ? 'copied ✓' : 'copy'}</button></p>
          <pre>{ticket.curl}</pre>
        </div>
      )}
    </div>
  )
}

const APPROVAL_LABEL: Record<string, string> = {
  pending: 'Waiting for approval', approved: 'Approved', rejected: 'Rejected', superseded: 'Superseded by a newer run', 'n/a': 'Failed',
}

function splitFrontmatter(text: string): { meta: [string, string][]; body: string } {
  const match = text.match(/^---\n([\s\S]*?)\n---\n?/)
  if (!match) return { meta: [], body: text }
  const meta = match[1].split('\n').map((line) => {
    const i = line.indexOf(':')
    return [line.slice(0, i).trim(), line.slice(i + 1).trim()] as [string, string]
  }).filter(([k]) => k)
  return { meta, body: text.slice(match[0].length) }
}

interface Props { runId: number; actingUser: User | undefined; onChanged: () => void; onOpenSession: (id: number) => void }

export function RunReview({ runId, actingUser, onChanged, onOpenSession }: Props) {
  const [run, setRun] = useState<DiscoveryRun | null>(null)
  const [tab, setTab] = useState<number | null>(null)
  const [content, setContent] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [polls, setPolls] = useState(0)  // bump to restart polling (after approve / retry)
  useEffect(() => {
    let stop = false
    const load = async () => {
      try {
        const r = await api.run(runId)
        if (stop) return
        setRun(r)
        setTab((t) => t ?? r.artifacts[0]?.id ?? null)
        if (r.status === 'running' || r.handoff?.status === 'pending') setTimeout(load, 2000)
        else if (polls > 0) onChanged()
      } catch (e) { if (!stop) setError((e as Error).message) }
    }
    load()
    return () => { stop = true }
  }, [runId, polls, onChanged])

  useEffect(() => {
    if (tab === null) return
    api.artifact(tab).then((a) => setContent(a.content)).catch((e: Error) => setError(e.message))
  }, [tab])

  const decide = async (action: 'approve' | 'reject') => {
    let reason = ''
    if (action === 'reject') {
      reason = window.prompt('Why are you rejecting this discovery run?') ?? ''
      if (!reason.trim()) return
    }
    setBusy(true); setError(null)
    try {
      setRun(action === 'approve' ? await api.approveRun(runId) : await api.rejectRun(runId, reason))
      setPolls((p) => p + 1)
      onChanged()
    } catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }

  const retry = async () => {
    setBusy(true); setError(null)
    try { setRun(await api.retryHandoff(runId)); setPolls((p) => p + 1) }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }

  if (!run) return <p className="hint">{error ?? 'Loading run…'}</p>

  const isStarter = actingUser?.id === run.started_by
  const canReview = actingUser?.role === 'approver' || actingUser?.role === 'owner'
  const awaiting = run.status === 'completed' && run.approval === 'pending'
  const { meta, body } = splitFrontmatter(content)

  return (
    <div className="review">
      <header className="review-head">
        <div>
          <h3>Discovery #{run.number}</h3>
          <p className="hint">
            {run.skills.join(' → ')} · started by {run.started_by_name} {new Date(run.started_at).toLocaleString()} ·{' '}
            <button className="link" onClick={() => onOpenSession(run.session_id)}>open session</button>
          </p>
        </div>
        <span className={`chip status-${run.status === 'completed' ? run.approval : run.status}`}>
          {run.status === 'running' ? `Running ${run.current_skill}…` : APPROVAL_LABEL[run.approval]}
        </span>
      </header>

      {run.status === 'failed' && <p className="banner error">This run failed: {run.error}</p>}
      {run.approval === 'rejected' && <p className="banner warn">Rejected: {run.rejection_reason}</p>}
      {run.approval === 'approved' && (
        <HandoffPanel runId={run.id} approvedBy={run.approved_by_name} handoff={run.handoff} busy={busy} onRetry={retry} />
      )}
      {run.status === 'completed' && run.approval === 'pending' && <TicketPreviewBox runId={run.id} />}
      {run.status === 'completed' && run.artifacts.some((a) => a.doc_type === 'CREW_TICKET') && (
        <CrewPanel run={run} onSent={() => setPolls((p) => p + 1)} />
      )}
      {awaiting && (
        <div className="decision">
          <span>Review the files below, then decide.</span>
          <button className="primary" disabled={busy || isStarter || !canReview} onClick={() => decide('approve')}
            title={isStarter ? 'You started this run, so someone else must approve it' : !canReview ? 'Switch "Acting as" to an approver' : ''}>Approve</button>
          <button disabled={busy || isStarter || !canReview} onClick={() => decide('reject')}>Reject…</button>
          {(isStarter || !canReview) && <small className="hint">{isStarter ? 'You started this run — another person must review it.' : 'Only an approver or owner can review.'}</small>}
        </div>
      )}
      {error && <p className="banner error" role="alert">{error}</p>}

      {run.artifacts.length === 0
        ? <p className="hint">{run.status === 'running' ? 'Files will appear here as each skill finishes.' : 'No files were produced.'}</p>
        : (
          <>
            <div className="tabs" role="tablist">
              {run.artifacts.map((a) => (
                <button key={a.id} role="tab" aria-selected={tab === a.id} className={tab === a.id ? 'tab active' : 'tab'} onClick={() => setTab(a.id)}>
                  {a.artifact_id} <small>v{a.version}</small>
                </button>
              ))}
            </div>
            {tab !== null && (
              <article className="artifact-view">
                <div className="artifact-meta">
                  {meta.filter(([k]) => ['artifact-id', 'version', 'status', 'parent-artifact', 'skill', 'approved-by'].includes(k))
                    .map(([k, v]) => <span key={k}><b>{k}</b> {v || '—'}</span>)}
                  <a href={api.downloadUrl(tab)}>Download .md</a>
                </div>
                <Markdown remarkPlugins={[remarkGfm]}>{body}</Markdown>
              </article>
            )}
          </>
        )}
    </div>
  )
}

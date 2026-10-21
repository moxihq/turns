// moxi-turns-gate, the gate (design §5.4 step 4, §5.5, §5.7).
//
// It acts only on what the moxi-turns runner submitted or queued, and passes everything
// else untouched: the person's prompts and typed /clear and /exit, other plugins' runs.
//
// RULE (entry, §5.7): a runner prompt enters only as the prompt of the window now open,
// admitted once by the endpoint; every other runner prompt is dropped at entry, logged, and
// never runs. A plugin's submit passes every hook but its own, so this gate's prompt.submit
// hook sees every runner prompt, told by its origin, as it enters. The runner puts its
// window's nonce on the prompt's first line (`moxi-turns-window: <nonce>`) and keeps the
// endpoint's mod.sock path in $.state (`endpoint`). For each runner prompt the gate reads
// the prompt box and the subagents and asks the endpoint (POST /gate/entry {nonce, draft,
// agents}); the endpoint admits it only as the open window's, once, re-checks a start there
// (§5.4 step 4), and records its answer before it sends it. On a pass the gate takes the
// nonce line off and calls `next({ ...e, text })`, keeping the origin (so the model reads
// the plugin's frame, not the person's words) and the context. Every other answer is
// `{ drop: text }` without `next`, so the prompt never enters: the endpoint's line, or the
// gate's own "moxi-turns-gate: a moxi-turns prompt was dropped (gone)" when there is no
// mod.sock path, no nonce line, no answer within 1 s (the gate's own timer), or a fault in
// this hook before `next`. The person sees the one line, the runner reads the reason from
// it, and the gate logs each drop it decides itself.
//
// A session command's re-check (RULE T4). For a /clear or /exit the runner queued (its
// `command` mark), the gate reads the subagents and the box, then asks the endpoint
// (POST /gate/command {request, agents, draft}), which records the decision before the
// gate passes; a drop, or no answer within 1 s (`gone`), withdraws the run without `next`,
// its one line "moxi-turns-gate: /<name> was refused (<reason>)".
//
// Calls: $.state.get (the runner's two values), $.http.fetch (the endpoint's mod.sock),
// $.agent.list, $.prompt.read, $.clock.after, $.command.register, $.ui.log.

import type { EngineInterface, PromptOrigin, Register } from 'claude-code'

type Dollar = EngineInterface
type Json = Record<string, unknown>

const RUNNER = 'moxi-turns'
const GATE = 'moxi-turns-gate'
const ASK_MS = 1000
const BUSY_AGENT = new Set(['pending', 'running', 'waiting'])
const ENDPOINT = { plugin: 'moxi-turns', key: 'endpoint' } as const
const COMMAND = { plugin: 'moxi-turns', key: 'command' } as const
/** The runner prompt's first line: the window nonce, 128 bits in hex. */
const NONCE = /^moxi-turns-window: ([0-9a-f]{32})(?:\n|$)/

const isRunner = (origin: PromptOrigin): boolean => origin.kind === 'plugin' && origin.name === RUNNER

const dropLine = (reason: string): string => `${GATE}: a moxi-turns prompt was dropped (${reason})`
const withdrawn = (name: string, reason: string) => ({ text: `${GATE}: /${name} was refused (${reason})` })

const log = ($: Dollar, text: string): void => {
  try {
    $.ui.log(`${GATE}: ${text}`, { to: 'debug' })
  } catch {
    // nowhere to say it
  }
}

/** A drop the gate decides itself, logged: the endpoint heard nothing of it. */
const gateDrop = ($: Dollar, why: string) => {
  log($, `dropped a moxi-turns prompt (gone): ${why}`)
  return { drop: dropLine('gone') }
}

type Readings = { draft: boolean | null; agents: number | true | null }

/** The live facts the endpoint decides with, read only where asked: the box (an unreadable
 *  one reads as a draft) and the subagents pending, running or waiting (an unreadable list
 *  reads `true`, busy). */
async function readings($: Dollar, draft: boolean, agents: boolean): Promise<Readings> {
  const out: Readings = { draft: null, agents: null }
  if (agents) {
    try {
      out.agents = (await $.agent.list()).filter(a => BUSY_AGENT.has(String(a.status))).length
    } catch {
      out.agents = true
    }
  }
  if (draft) {
    try {
      out.draft = (await $.prompt.read()).text !== ''
    } catch {
      out.draft = true
    }
  }
  return out
}

/** The endpoint's answer, or undefined when it missed the gate's 1 s bound or failed. */
async function ask($: Dollar, sock: string, path: string, body: Json): Promise<Json | undefined> {
  const sent = $.http
    .fetch(`http://moxi-turns${path}`, {
      method: 'POST',
      socketPath: sock,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    .then(r => {
      if (!r.ok) return undefined
      const v = r.text ? JSON.parse(r.text) : {}
      return v !== null && typeof v === 'object' ? (v as Json) : undefined
    })
    .catch(() => undefined)
  const late = new Promise<undefined>(resolve => { $.clock.after(ASK_MS, () => resolve(undefined)) })
  return Promise.race([sent, late])
}

/** The endpoint's verdict: null to pass, else the one line to answer with. */
function verdict(answer: Json | undefined, refusedLine: (reason: string) => string): string | null {
  if (answer === undefined) return refusedLine('gone')
  if (answer.verdict === 'pass') return null
  if (typeof answer.text === 'string' && answer.text !== '') return answer.text
  return refusedLine(typeof answer.reason === 'string' && answer.reason !== '' ? answer.reason : 'gone')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'moxi-turns-gate', description: 'moxi-turns-gate is loaded (runs nothing)' })
    return next(e)
  })

  // The presence check the runner reads in $.command.list(): hidden, and it runs nothing.
  on('command.describe', { command: 'moxi-turns-gate' }, async ($, e, next) => ({ ...(await next(e)), isHidden: true }))
  on('command.run', { command: 'moxi-turns-gate' }, () => ({ text: `${GATE}: loaded` }))

  on('prompt.submit', async ($, e, next) => {
    if (!isRunner(e.origin)) return next(e)
    const mark = (await $.state.get(ENDPOINT)).value
    if (mark === undefined || mark === null) return gateDrop($, 'no mod.sock path in $.state')
    const nonce = NONCE.exec(e.text)
    if (nonce === null) return gateDrop($, 'no window nonce on its first line')
    const read = await readings($, true, true)
    const answer = await ask($, mark.sock, '/gate/entry', { nonce: nonce[1], ...read })
    if (answer === undefined) return gateDrop($, 'the endpoint did not answer within 1 s')
    const line = verdict(answer, dropLine)
    if (line !== null) return { drop: line }
    // Admitted: the nonce line comes off; the origin and the context stay (§5.7).
    return next({ ...e, text: e.text.slice(nonce[0].length) })
  }).catch(($, e, next) => (next.called || !isRunner(e.origin) ? next(e) : gateDrop($, 'a fault in the hook')))

  on('command.run', { command: ['clear', 'exit'] }, async ($, e, next) => {
    if (!isRunner(e.origin)) return next(e)
    const mark = (await $.state.get(COMMAND)).value
    if (mark === undefined || mark === null || mark.name !== e.command) return withdrawn(e.command, 'no-pending')
    const read = await readings($, true, true)
    const answer = await ask($, mark.sock, '/gate/command', { request: mark.request, ...read })
    const line = verdict(answer, reason => withdrawn(e.command, reason).text)
    return line === null ? next(e) : { text: line }
  }).catch(($, e, next) => (next.called || !isRunner(e.origin) ? next(e) : withdrawn(e.command, 'gone')))
}

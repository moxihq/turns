// moxi-turns, the runner: turns' Claude Code adapter in the session (design §5.7).
//
// It spawns the session's endpoint (`endpoint.json` beside this plugin names the command),
// reports the session's main-loop turns to it, puts in what it hands over (a submit, a
// steer, a /clear or /exit), and asks it at SessionStart, UserPromptSubmit and Stop. The
// runner reads no setting: its bounds are the constants below, and the endpoint's first
// line carries `hold_wait_ms` and `listen_wait_ms`.
//
// The internal leg (§5.2: not the socket contract; it changes only with the package). The
// endpoint's half, moxi_turns.claude_leg, states it in full; the runner's part:
//
// Endpoint to runner: one JSON object per line on the child's stdout, by `op`:
//   hello    {op, leg: 1, mod_sock, hold_wait_ms, listen_wait_ms, endpoint_version, pid}
//            the first line. Before it the runner sends nothing, and it sends nothing to an
//            endpoint whose leg it does not speak.
//   failed   {op, leg, error}   the endpoint could not start: its only line
//   submit   {op, delivery, kind, text, nonce}
//            opens the submit's window and calls $.prompt.submit with the line
//            `moxi-turns-window: <nonce>` first (entry, below); one window at a time
//   append   {op, delivery, text}             a steer: $.session.append, a user row
//   live     {op, ask}                        the live facts: answered on POST /live
//   command  {op, request, name: clear|exit}  $.command.run, which the gate re-checks
//
// Runner to endpoint: HTTP POST with a JSON body to `mod_sock`, answered with a JSON object.
//   /report  {seq, kind, ...} through one ordered queue, each sent once the one before it
//            landed; `seq` counts from 1 per runner load, in the order sent. Kinds:
//              session        {session_id, mod_version}  first, once the hello came
//              turn.start     {turn_id}
//              turn.complete  {turn_id, reason, is_aborted}
//              submit.close   {delivery, end, reason}  the submit settled: end is resolve,
//                             drop (reason: the gate's code, else `dropped`; `unsupported`
//                             when the gate is not loaded and nothing was submitted) or
//                             fail (reason: words). Sent whenever it settles, after a
//                             bound's report too.
//              submit.bound   {delivery, reason}  the runner's bound (below)
//              append.failed  {delivery, reason}
//              command.done   {request, state: ran|refused|failed, reason, new_session_id}
//   /ask/context {event: ready|prompt, source, session_id}  -> {context: string | null}
//   /ask/stop    {turn_id, session_id, stop_hook_active, task_types, crons}
//                                                          -> {reason: string | null}
//   /live        {ask, agents, draft}                      -> {}  (agents null: unreadable)
//   The gate asks on the same socket: /gate/entry {nonce, draft, agents} and
//   /gate/command {request, agents, draft} -> {verdict: pass} | {verdict: drop, reason,
//   text}; `draft` and `agents` are the gate's readings.
//
// RULE (the internal leg): no hook waits on the endpoint longer than a bound this plugin's
// own timer keeps, and a request that misses its bound counts as unanswered. A reporting
// hook waits at most 1 s for its report; the Stop ask hold_wait_ms + 1 s; a context ask
// listen_wait_ms + 1 s; SessionStart waits at most 2 s for the first line.
//
// The queue: a report made before the first line waits in the queue and is sent after the
// session report once the line comes; one whose hook stopped waiting (the 1 s) stays queued
// and is still sent, in order. A send that fails while the endpoint lives is tried again
// (5 tries, 100 ms apart, the same `seq`), then dropped and logged so the reports behind it
// still go; the endpoint reads the gap in `seq` and fails an open window rather than tie it. When
// the endpoint has ended the queue is dropped.
//
// RULE (the tie, §5.7): each plugin submit has one window, which closes exactly once, at the
// first end the endpoint accepts: resolve (the submit resolved with its prompt entered),
// drop (it resolved `{ drop }`), fail (it rejected, or is still unsettled at the runner's
// bound while its prompt is not admitted, or the endpoint ended). The runner reports each
// main-loop turn.start inside its hook before the submit can resolve, and the settle after
// it, through the one queue; the endpoint ties. The endpoint alone decides the close, so
// the runner reports and decides nothing: a new `submit` line means the endpoint closed the
// last window, and the last submit, if still pending, reports its settle when it comes.
// The bound: a submit still pending after 5 s with no main-loop turn open is reported once
// (`submit.bound`). The time counts only while no turn is open: from the call when idle,
// stopped at each turn.start, started again from zero at each turn.complete. While turns'
// own entry hook for the prompt runs (UserPromptSubmit, which asks the listeners), the
// bound is raised by that ask's own bound.
//
// RULE (entry, §5.7): a runner prompt enters only as the prompt of the window now open,
// admitted once by the endpoint. The runner puts the window nonce on the prompt's first
// line and keeps the endpoint's mod.sock path in $.state (`endpoint`), written at the
// first line, at every SessionStart (a /clear empties $.state) and before each submit. The
// gate (moxi-turns-gate) asks the endpoint for every runner prompt, takes the nonce line
// off on a pass and drops everything else. The gate is required: without it in
// $.command.list() the runner submits nothing, and the submit closes as `unsupported`.
//
// RULE (non-interactive runs): `$.session.surfaces()` empty means headless, read in
// whichever of session.start and classic.SessionStart runs first; the first read decides.
// A headless run spawns no endpoint and every hook returns at once.

import type { EngineInterface, Register } from 'claude-code'

type Dollar = EngineInterface

const MOD_VERSION = '0.1.0'
const GATE = 'moxi-turns-gate'
const MARGIN_MS = 1000
const REPORT_WAIT_MS = 1000
const HELLO_WAIT_MS = 2000
const IDLE_FAIL_MS = 5000
const SEND_TRIES = 5
const SEND_RETRY_MS = 100
const QUEUE_MAX = 1000
const BUSY_AGENT = new Set(['pending', 'running', 'waiting'])
const GATE_DROP = /^moxi-turns-gate: .*\(([a-z-]+)\)$/
const NONCE_LINE = 'moxi-turns-window: '
const LEG = 1

const ENDPOINT = { plugin: 'moxi-turns', key: 'endpoint' } as const
const COMMAND = { plugin: 'moxi-turns', key: 'command' } as const

type Hello = { sock: string; holdWaitMs: number; listenWaitMs: number }
type Json = Record<string, unknown>
type Report = { kind: string; fields: Json; seq: number; landed: Promise<void>; land: () => void }
type Win = {
  delivery: string
  /** The submit settled and its settle was reported (or the endpoint ended). */
  closed: boolean
  /** The bound was reported; it is reported once. */
  bounded: boolean
  timer: { cancel: () => void } | null
}

// Module state: a reload starts it over, with a new endpoint (§5.7, the endpoint's life).
let headless: boolean | undefined
let spawned = false
let hello: Hello | undefined
let ended = false
let helloSeen: () => void = () => {}
let helloP = new Promise<void>(resolve => { helloSeen = resolve })
let turnOpen: string | null = null
let win: Win | null = null
let seq = 0
let queue: Report[] = []
let sending = false

const log = ($: Dollar, text: string): void => {
  try {
    $.ui.log(`moxi-turns: ${text}`, { to: 'debug' })
  } catch {
    // nowhere to say it
  }
}

/** A promise that resolves undefined after `ms`, on the plugin's own timer. */
function after<T>($: Dollar, ms: number): Promise<T | undefined> {
  return new Promise(resolve => { $.clock.after(ms, () => resolve(undefined)) })
}

async function post($: Dollar, sock: string, path: string, body: Json): Promise<Json> {
  const r = await $.http.fetch(`http://moxi-turns${path}`, {
    method: 'POST',
    socketPath: sock,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  const v = r.text ? JSON.parse(r.text) : {}
  return v !== null && typeof v === 'object' ? (v as Json) : {}
}

/** An ask: the answer, or undefined when there is no endpoint or it missed `boundMs`. */
async function ask($: Dollar, path: string, body: Json, boundMs: number): Promise<Json | undefined> {
  if (hello === undefined || ended) return undefined
  const sent = post($, hello.sock, path, body).catch(err => {
    log($, `${path} failed: ${String(err)}`)
    return undefined
  })
  return Promise.race([sent, after<Json>($, boundMs)])
}

// -- the report queue -------------------------------------------------------------------

function report($: Dollar, kind: string, fields: Json): Promise<void> {
  let land: () => void = () => {}
  const landed = new Promise<void>(resolve => { land = resolve })
  if (ended) return Promise.resolve()
  if (queue.length >= QUEUE_MAX) {
    log($, `report queue full; ${kind} dropped`)
    return Promise.resolve()
  }
  queue.push({ kind, fields, seq: 0, landed, land })
  void pump($)
  return landed
}

/** Reports and waits for it to land, at most 1 s; none of that before the first line. */
async function reportAndWait($: Dollar, kind: string, fields: Json): Promise<void> {
  const landed = report($, kind, fields)
  if (hello === undefined) return
  await Promise.race([landed, after($, REPORT_WAIT_MS)])
}

async function pump($: Dollar): Promise<void> {
  if (sending || hello === undefined) return
  sending = true
  try {
    while (queue.length > 0 && !ended && hello !== undefined) {
      const item = queue[0]
      if (item === undefined) break
      let tries = 0
      if (item.seq === 0) {
        seq += 1
        item.seq = seq
      }
      for (;;) {
        try {
          await post($, hello.sock, '/report', { seq: item.seq, kind: item.kind, ...item.fields })
          break
        } catch (err) {
          tries += 1
          if (ended) break
          if (tries >= SEND_TRIES) {
            log($, `report ${item.kind} dropped after ${tries} tries: ${String(err)}`)
            break
          }
          await after($, SEND_RETRY_MS)
        }
      }
      queue.shift()
      item.land()
    }
  } finally {
    sending = false
  }
  if (ended) dropQueue()
}

function dropQueue(): void {
  for (const item of queue) item.land()
  queue = []
}

// -- the window (the tie) -----------------------------------------------------------------

function arm($: Dollar, w: Win, ms: number): void {
  w.timer?.cancel()
  w.timer = null
  if (w.closed || w.bounded || win !== w || turnOpen !== null) return
  w.timer = $.clock.after(ms, () => {
    if (w.closed || w.bounded || win !== w || turnOpen !== null) return
    w.bounded = true
    w.timer = null
    // A report, not a close: the endpoint fails the window by it only while its prompt
    // is not admitted. The submit's settle is still reported when it comes.
    void report($, 'submit.bound', { delivery: w.delivery, reason: `unsettled after ${IDLE_FAIL_MS} ms with no turn open` })
  })
}

function stopTimer(w: Win | null): void {
  w?.timer?.cancel()
  if (w) w.timer = null
}

function settle($: Dollar, w: Win, end: 'resolve' | 'drop' | 'fail', reason: string | null): void {
  if (w.closed) return
  w.closed = true
  stopTimer(w)
  if (win === w) win = null
  void report($, 'submit.close', { delivery: w.delivery, end, reason })
}

/** The refusal reason a drop names: the gate's code, else `dropped`. */
function dropReason(text: string): string {
  const m = GATE_DROP.exec(text)
  return m?.[1] ?? 'dropped'
}

async function gateLoaded($: Dollar): Promise<boolean> {
  try {
    return (await $.command.list()).some(c => c.name === GATE)
  } catch {
    return false
  }
}

/** The endpoint's mod.sock path, kept in $.state for the gate. */
async function writeSock($: Dollar): Promise<void> {
  if (hello === undefined || ended) return
  await $.state.set(ENDPOINT, { sock: hello.sock })
}

async function openWindow($: Dollar, delivery: string, text: string, nonce: string): Promise<void> {
  // A new submit means the endpoint closed the last window. If that submit is still
  // pending it reports its settle when it comes; its bound no longer counts.
  if (win !== null) {
    stopTimer(win)
    win = null
  }
  if (!(await gateLoaded($))) {
    void report($, 'submit.close', { delivery, end: 'drop', reason: 'unsupported' })
    return
  }
  try {
    await writeSock($)
  } catch (err) {
    void report($, 'submit.close', { delivery, end: 'fail', reason: `the mod.sock path was not written: ${String(err)}` })
    return
  }
  const w: Win = { delivery, closed: false, bounded: false, timer: null }
  win = w
  arm($, w, IDLE_FAIL_MS)
  let submitted: ReturnType<Dollar['prompt']['submit']>
  try {
    submitted = $.prompt.submit({ text: `${NONCE_LINE}${nonce}\n${text}` })
  } catch (err) {
    settle($, w, 'fail', `rejected: ${String(err)}`)
    return
  }
  submitted.then(
    r => {
      if (w.closed) return
      if (w.bounded) log($, `submit ${delivery} settled after its bound`)
      if (typeof r.drop === 'string') settle($, w, 'drop', dropReason(r.drop))
      else settle($, w, 'resolve', null)
    },
    err => {
      if (!w.closed) settle($, w, 'fail', `rejected: ${String(err)}`)
    },
  )
}

// -- the endpoint's lines -------------------------------------------------------------------

const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)
const num = (v: unknown, d: number): number => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : d)

async function live($: Dollar, askId: unknown): Promise<void> {
  // An unreadable subagent list is unknown, sent as null, which the endpoint counts as
  // busy: never as 0 (§5.7, idle facts). An unreadable box counts as a draft.
  let agents: number | null = null
  let draft = true
  try {
    agents = (await $.agent.list()).filter(a => BUSY_AGENT.has(String(a.status))).length
  } catch {
    agents = null
  }
  try {
    draft = (await $.prompt.read()).text !== ''
  } catch {
    draft = true // an unreadable box counts as a draft
  }
  await ask($, '/live', { ask: askId, agents, draft }, REPORT_WAIT_MS)
}

async function append($: Dollar, delivery: string, text: string): Promise<void> {
  try {
    const r = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    const deny = (r as { deny?: string }).deny
    if (deny !== undefined) void report($, 'append.failed', { delivery, reason: deny })
  } catch (err) {
    void report($, 'append.failed', { delivery, reason: String(err) })
  }
}

async function command($: Dollar, request: string, name: 'clear' | 'exit'): Promise<void> {
  if (!(await gateLoaded($))) {
    void report($, 'command.done', { request, state: 'refused', reason: 'unsupported', new_session_id: null })
    return
  }
  try {
    await $.state.set(COMMAND, { request, name, sock: hello!.sock })
  } catch (err) {
    void report($, 'command.done', { request, state: 'failed', reason: `the command's mark was not written: ${String(err)}`, new_session_id: null })
    return
  }
  let text: string | null = null
  let error: string | null = null
  try {
    text = (await $.command.run({ command: name })).text ?? null
  } catch (err) {
    error = String(err)
  }
  let sessionId: string | null = null
  try {
    sessionId = await $.session.id()
  } catch {
    sessionId = null
  }
  // The gate's withdrawal is the command's own answer, one line naming the reason.
  const withdrawn = text === null ? null : GATE_DROP.exec(text)
  if (error !== null) void report($, 'command.done', { request, state: 'failed', reason: error, new_session_id: null })
  else if (withdrawn) void report($, 'command.done', { request, state: 'refused', reason: withdrawn[1] ?? 'gone', new_session_id: null })
  else void report($, 'command.done', { request, state: 'ran', reason: null, new_session_id: sessionId })
  void $.state.set(COMMAND, null).catch(() => {})
}

async function onLine($: Dollar, line: string): Promise<void> {
  let m: Json
  try {
    const v = JSON.parse(line)
    if (v === null || typeof v !== 'object') throw new Error('not an object')
    m = v as Json
  } catch {
    log($, `the endpoint wrote a line that is not a JSON object: ${line.slice(0, 120)}`)
    return
  }
  const op = m.op
  if (op === 'failed') {
    endpointEnded($, `it could not start: ${String(m.error)}`)
    return
  }
  if (op === 'hello') {
    const sock = str(m.mod_sock)
    if (hello !== undefined || sock === undefined) return
    if (m.leg !== LEG) {
      endpointEnded($, `it speaks leg ${String(m.leg)}, the runner leg ${LEG}`)
      return
    }
    let sessionId: string | null = null
    try {
      sessionId = await $.session.id()
    } catch {
      sessionId = null
    }
    // The session report goes first, ahead of anything queued before the line came.
    let land: () => void = () => {}
    const landed = new Promise<void>(resolve => { land = resolve })
    queue.unshift({ kind: 'session', fields: { session_id: sessionId, mod_version: MOD_VERSION }, seq: 0, landed, land })
    hello = { sock, holdWaitMs: num(m.hold_wait_ms, 2000), listenWaitMs: num(m.listen_wait_ms, 2000) }
    helloSeen()
    void pump($)
    void writeSock($).catch(err => log($, `the mod.sock path was not written: ${String(err)}`))
    return
  }
  if (hello === undefined) return
  const delivery = str(m.delivery)
  const nonce = str(m.nonce)
  if (op === 'submit' && delivery !== undefined) {
    if (nonce === undefined || nonce === '') {
      // Without a nonce the gate would drop the prompt: submit nothing, say so.
      void report($, 'submit.close', { delivery, end: 'fail', reason: 'the submit line carries no window nonce' })
    } else {
      await openWindow($, delivery, str(m.text) ?? '', nonce)
    }
  } else if (op === 'append' && delivery !== undefined) {
    await append($, delivery, str(m.text) ?? '')
  } else if (op === 'live') {
    await live($, m.ask)
  } else if (op === 'command' && str(m.request) !== undefined && (m.name === 'clear' || m.name === 'exit')) {
    await command($, m.request as string, m.name)
  } else {
    log($, `the endpoint wrote an op the runner does not take: ${String(op)}`)
  }
}

function endpointEnded($: Dollar, why: string): void {
  if (ended) return
  ended = true
  log($, `the endpoint ended: ${why}`)
  dropQueue()
  if (win !== null) {
    const w = win
    w.closed = true
    stopTimer(w)
    win = null
  }
  void $.state.set(ENDPOINT, null).catch(() => {})
  void $.state.set(COMMAND, null).catch(() => {})
  helloSeen()
}

async function readArgv($: Dollar): Promise<string[] | undefined> {
  let doc: unknown
  try {
    doc = JSON.parse(await $.fs.read(`${$.plugin.root}/endpoint.json`))
  } catch {
    log($, `no endpoint: ${$.plugin.root}/endpoint.json is missing or not JSON (run turns' setup)`)
    return undefined
  }
  const d = (doc ?? {}) as Json
  const argv = Array.isArray(d.argv) ? d.argv : typeof d.command === 'string' ? [d.command] : undefined
  if (argv === undefined || argv.length === 0 || !argv.every(a => typeof a === 'string') || !(argv[0] as string).startsWith('/')) {
    log($, 'no endpoint: endpoint.json names no absolute command')
    return undefined
  }
  return argv as string[]
}

async function spawn($: Dollar): Promise<void> {
  if (spawned) return
  spawned = true
  const argv = await readArgv($)
  if (argv === undefined) {
    endpointEnded($, 'no endpoint command')
    return
  }
  void (async () => {
    let buf = ''
    try {
      const child = $.process.spawn({ argv })
      for (;;) {
        const x = await child.next()
        if (x.done) {
          endpointEnded($, `exited ${JSON.stringify(x.value)}`)
          break
        }
        const { stream, text } = x.value
        if (stream !== 'stdout') continue
        buf += text
        let i: number
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i)
          buf = buf.slice(i + 1)
          if (line.trim() !== '') void onLine($, line)
        }
      }
    } catch (err) {
      endpointEnded($, `could not run: ${String(err)}`)
    }
  })()
}

/** The first surfaces read decides: true when the session draws nowhere. */
async function isHeadless($: Dollar): Promise<boolean> {
  if (headless === undefined) {
    let surfaces: readonly string[] = []
    try {
      surfaces = await $.session.surfaces()
    } catch {
      surfaces = []
    }
    if (headless === undefined) headless = surfaces.length === 0
  }
  return headless
}

const withContext = <R extends { additionalContext?: string[] }>(r: R, context: unknown): R =>
  typeof context === 'string' && context !== '' ? { ...r, additionalContext: [...(r.additionalContext ?? []), context] } : r

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // Spawned before `next`, so the endpoint's start overlaps the session's (PB9).
    if (!(await isHeadless($))) await spawn($)
    return next(e)
  })

  on('classic.SessionStart', async ($, e, next) => {
    if (e.agent_id !== undefined || (await isHeadless($))) return next(e)
    if (!spawned) await spawn($)
    if (hello === undefined && !ended) await Promise.race([helloP, after($, HELLO_WAIT_MS)])
    if (hello === undefined || ended) return next(e)
    // A /clear empties $.state (and a resume or compaction may): the mod.sock path is
    // written again at every SessionStart, for the gate.
    await writeSock($).catch(err => log($, `the mod.sock path was not written: ${String(err)}`))
    const answer = await ask($, '/ask/context', { event: 'ready', source: e.source ?? null, session_id: e.session_id }, hello.listenWaitMs + MARGIN_MS)
    return withContext(await next(e), answer?.context)
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    if (e.agent_id !== undefined || headless === true || hello === undefined || ended) return next(e)
    const w = win
    // turns' own entry hook: the bound is raised by this ask's bound while it runs.
    if (w !== null && turnOpen === null) arm($, w, IDLE_FAIL_MS + hello.listenWaitMs + MARGIN_MS)
    const answer = await ask($, '/ask/context', { event: 'prompt', source: null, session_id: e.session_id }, hello.listenWaitMs + MARGIN_MS)
    if (w !== null && win === w && turnOpen === null) arm($, w, IDLE_FAIL_MS)
    return withContext(await next(e), answer?.context)
  })

  on('turn.start', async ($, e, next) => {
    if (headless === true || ended) return next(e)
    turnOpen = e.turnId
    stopTimer(win)
    await reportAndWait($, 'turn.start', { turn_id: e.turnId })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined || headless === true || ended) return next(e)
    turnOpen = null
    await reportAndWait($, 'turn.complete', { turn_id: e.turnId, reason: e.reason, is_aborted: e.isAborted })
    if (win !== null) arm($, win, IDLE_FAIL_MS)
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    if (e.agent_id !== undefined || headless === true || hello === undefined || ended) return next(e)
    const facts = {
      turn_id: turnOpen,
      session_id: e.session_id,
      stop_hook_active: e.stop_hook_active,
      task_types: e.background_tasks === undefined ? null : e.background_tasks.map(t => String((t as { type?: unknown }).type ?? 'unknown')),
      crons: e.session_crons === undefined ? null : e.session_crons.length,
    }
    const answer = await ask($, '/ask/stop', facts, hello.holdWaitMs + MARGIN_MS)
    const r = await next(e)
    const reason = answer?.reason
    return typeof reason === 'string' && reason !== '' ? { ...r, block: reason } : r
  })
}

// The runner: the non-interactive rule, the internal leg's queue and bounds,
// the tie's window with its three ends and the idle-only bound, the context asks, the Stop
// ask, and the session commands.

import { describe, expect, test } from 'claude-code/testing'
import { ROOT_ARGV, SOCK, world } from './world.ts'
import type { World } from './world.ts'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type E = any

const start = ($: E) => $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
const turnStart = ($: E, turnId: string) => $.turn.start({ text: '', turnId })
const turnEnd = ($: E, turnId: string, reason = 'answer') =>
  $.turn.complete({ answer: '', durationMs: 1, isAborted: reason === 'aborted', turnId, reason })

async function attached($: E, w: World): Promise<void> {
  await start($)
  await w.hello()
}

describe('the non-interactive rule', () => {
  test('a headless run spawns nothing, and SessionStart waits for no first line', async ($: E, on) => {
    const w = world(on, { surfaces: [] })
    // Under -p classic.SessionStart comes first; its read decides.
    const r = await $.classic.SessionStart({ source: 'startup' })
    await start($)
    await turnStart($, 't1')
    await turnEnd($, 't1')
    await $.classic.Stop({ stop_hook_active: false })
    expect(w.spawned).toEqual([])
    expect(w.got).toEqual([])
    expect(r.additionalContext ?? []).toEqual([])
    expect(w.clock.now()).toBe(1_000)
  })

  test('an interactive start spawns the endpoint named in endpoint.json, at session.start', async ($: E, on) => {
    const w = world(on)
    await start($)
    expect(w.spawned).toEqual([ROOT_ARGV])
  })

  test('with endpoint.json missing nothing is spawned and SessionStart does not wait', async ($: E, on) => {
    const w = world(on, { endpointJson: null })
    await start($)
    await $.classic.SessionStart({ source: 'startup' })
    expect(w.spawned).toEqual([])
    expect(w.clock.now()).toBe(1_000)
  })

  test('endpoint.json naming a relative command spawns nothing', async ($: E, on) => {
    const w = world(on, { endpointJson: JSON.stringify({ argv: ['moxi-turns-endpoint'] }) })
    await start($)
    expect(w.spawned).toEqual([])
  })
})

describe('the report queue', () => {
  test('nothing is sent before the first line; attach goes first, then what waited, in order', async ($: E, on) => {
    const w = world(on)
    await start($)
    await turnStart($, 't1')
    await turnEnd($, 't1')
    expect(w.got).toEqual([])
    await w.hello()
    expect(w.kinds()).toEqual(['session', 'turn.start', 'turn.complete'])
    expect(w.reports()[0]).toEqual({ seq: 1, kind: 'session', session_id: 'S-1', mod_version: '0.1.0' })
    expect(w.reports().map(r => r.seq)).toEqual([1, 2, 3])
    expect(w.reports()[2]).toEqual({ seq: 3, kind: 'turn.complete', turn_id: 't1', reason: 'answer', is_aborted: false })
  })

  test('a turn-start hook waits at most 1 s for its report, and the report still lands after', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.answers['/report'] = 'hang'
    const pending = turnStart($, 't1')
    await w.clock.advance(999)
    let done = false
    void pending.then(() => { done = true })
    await w.clock.settle()
    expect(done).toBe(false)
    await w.clock.advance(1)
    await pending
    expect(done).toBe(true)
  })

  test('a send that keeps failing is dropped after 5 tries, and the next report goes', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.answers['/report'] = 'fail'
    void turnStart($, 't1')
    await w.clock.advance(1000)
    const tries = w.got.filter(g => g.path === '/report' && g.body.kind === 'turn.start').length
    expect(tries).toBe(5)
    w.answers['/report'] = {}
    await turnEnd($, 't1')
    expect(w.kinds().at(-1)).toBe('turn.complete')
  })
})

describe('the context asks', () => {
  test('SessionStart waits for the first line, then hands back the listeners\' context', async ($: E, on) => {
    const w = world(on)
    await start($)
    w.answers['/ask/context'] = { context: 'ROSTER' }
    const r = $.classic.SessionStart({ source: 'startup' })
    await w.clock.advance(100)
    await w.hello()
    const out = await r
    expect(out.additionalContext).toEqual(['ROSTER'])
    expect(w.got.find(g => g.path === '/ask/context')?.body).toEqual({ event: 'ready', source: 'startup', session_id: expect.any(String) })
  })

  test('SessionStart gives up on the first line after 2 s', async ($: E, on) => {
    const w = world(on)
    await start($)
    const r = $.classic.SessionStart({ source: 'startup' })
    await w.clock.advance(2000)
    const out = await r
    expect(out.additionalContext ?? []).toEqual([])
    expect(w.got).toEqual([])
  })

  test('a context ask that misses listen_wait_ms + 1 s adds nothing', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.answers['/ask/context'] = 'hang'
    const r = $.classic.UserPromptSubmit({ prompt: 'hi' })
    await w.clock.advance(2999)
    let done = false
    void r.then(() => { done = true })
    await w.clock.settle()
    expect(done).toBe(false)
    await w.clock.advance(1)
    expect((await r).additionalContext ?? []).toEqual([])
  })
})

describe('the Stop ask', () => {
  test('a reason from the endpoint holds the turn open', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    await turnStart($, 't1')
    w.answers['/ask/stop'] = { reason: 'reply first' }
    const r = await $.classic.Stop({ stop_hook_active: false, background_tasks: [], session_crons: [] })
    expect(r.block).toBe('reply first')
    expect(w.got.find(g => g.path === '/ask/stop')?.body).toEqual({ turn_id: 't1', session_id: expect.any(String), stop_hook_active: false, task_types: [], crons: 0 })
  })

  test('an endpoint that does not answer within hold_wait_ms + 1 s lets the turn end', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.answers['/ask/stop'] = 'hang'
    const r = $.classic.Stop({ stop_hook_active: false })
    await w.clock.advance(3000)
    expect((await r).block).toBeUndefined()
  })
})

const N1 = 'a'.repeat(32)
const N2 = 'b'.repeat(32)
const submitLine = (delivery: string, text: string, nonce = N1, kind = 'deliver') => ({ op: 'submit', delivery, kind, text, nonce })
const closes = (w: World) => w.reports().filter(r => r.kind === 'submit.close')
const bounds = (w: World) => w.reports().filter(r => r.kind === 'submit.bound')

describe('the tie: one window per submit', () => {
  test('an idle submit: the nonce line, the path, its turn-start, then the close by resolve', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    expect(w.stateWrites).toContainEqual({ key: 'endpoint', value: { sock: SOCK } }) // at the first line
    w.settle = async () => {
      await turnStart($, 't1') // the prompt's own turn.start fires before the resolve
      return { text: 'x' }
    }
    const n = w.stateWrites.length
    await w.line(submitLine('d1', 'hello\nsecond line', N1, 'start'))
    expect(w.submits).toEqual([`moxi-turns-window: ${N1}\nhello\nsecond line`])
    expect(w.stateWrites.slice(n)).toEqual([{ key: 'endpoint', value: { sock: SOCK } }]) // again, before the submit
    expect(w.kinds()).toEqual(['session', 'turn.start', 'submit.close'])
    expect(w.reports()[2]).toMatchObject({ delivery: 'd1', end: 'resolve', reason: null })
  })

  test('a drop by the gate closes with the gate\'s reason; another plugin\'s drop reads dropped', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.settle = async () => ({ drop: 'moxi-turns-gate: the start was refused (stale-turn)' })
    await w.line(submitLine('d1', 'a', N1, 'start'))
    w.settle = async () => ({ drop: 'moxi-turns-gate: a moxi-turns prompt was dropped (gone)' })
    await w.line(submitLine('d2', 'b', N2))
    w.settle = async () => ({ drop: 'someone: no' })
    await w.line(submitLine('d3', 'c', 'c'.repeat(32)))
    expect(closes(w)).toMatchObject([
      { delivery: 'd1', end: 'drop', reason: 'stale-turn' },
      { delivery: 'd2', end: 'drop', reason: 'gone' },
      { delivery: 'd3', end: 'drop', reason: 'dropped' },
    ])
  })

  test('a rejected submit fails its window', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.settle = async () => { throw new Error('nope') }
    await w.line(submitLine('d1', 'a'))
    expect(closes(w)).toMatchObject([{ delivery: 'd1', end: 'fail' }])
  })

  test('with the gate not loaded nothing is submitted, a deliver or a start, and each closes as unsupported', async ($: E, on) => {
    const w = world(on, { gate: false })
    await attached($, w)
    await w.line(submitLine('d1', 'a', N1, 'start'))
    await w.line(submitLine('d2', 'b', N2))
    expect(w.submits).toEqual([])
    expect(closes(w)).toMatchObject([
      { delivery: 'd1', end: 'drop', reason: 'unsupported' },
      { delivery: 'd2', end: 'drop', reason: 'unsupported' },
    ])
  })

  test('a submit line with no nonce submits nothing and fails', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    await w.line({ op: 'submit', delivery: 'd1', kind: 'deliver', text: 'a' })
    expect(w.submits).toEqual([])
    expect(closes(w)).toMatchObject([{ delivery: 'd1', end: 'fail' }])
  })

  test('the bound is a report: once at 5 s idle, not before; the settle is still reported after it', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    let resolve: (v: Record<string, unknown>) => void = () => {}
    w.settle = () => new Promise(r => { resolve = r })
    await w.line(submitLine('d1', 'a'))
    await w.clock.advance(4999)
    expect(bounds(w)).toEqual([])
    await w.clock.advance(1)
    expect(bounds(w)).toMatchObject([{ delivery: 'd1', reason: 'unsettled after 5000 ms with no turn open' }])
    expect(closes(w)).toEqual([])
    await w.clock.advance(20_000)
    expect(bounds(w).length).toBe(1)
    resolve({ drop: 'moxi-turns-gate: a late prompt was dropped (late)' }) // the dialog closed
    await w.clock.settle()
    expect(closes(w)).toMatchObject([{ delivery: 'd1', end: 'drop', reason: 'late' }])
  })

  test('the bound counts only idle time: stopped by a turn.start, from zero again at its turn.complete', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    await turnStart($, 't0') // a turn runs: the submit queues behind it
    w.settle = () => new Promise(() => {})
    await w.line(submitLine('d1', 'a'))
    await w.clock.advance(20_000)
    expect(bounds(w)).toEqual([])
    await turnEnd($, 't0')
    await w.clock.advance(4999)
    expect(bounds(w)).toEqual([])
    await w.clock.advance(1)
    expect(bounds(w).length).toBe(1)
  })

  test('the bound is raised while turns\' UserPromptSubmit ask runs', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.answers['/ask/context'] = 'hang'
    w.settle = () => new Promise(() => {})
    await w.line(submitLine('d1', 'a'))
    await w.clock.advance(4000)
    void $.classic.UserPromptSubmit({ prompt: 'a' }) // the prompt is entering; its ask takes 3 s
    await w.clock.advance(2000) // 6 s from the call
    expect(bounds(w)).toEqual([])
    await w.clock.advance(1000) // the ask gave up; the bound starts again from zero
    await w.clock.advance(4999)
    expect(bounds(w)).toEqual([])
    await w.clock.advance(1)
    expect(bounds(w).length).toBe(1)
  })

  test('a new submit line while the last one is pending: the last reports its settle later, its bound no more', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    const pending: ((v: Record<string, unknown>) => void)[] = []
    w.settle = () => new Promise(r => { pending.push(r) })
    await w.line(submitLine('d1', 'same'))
    await w.clock.advance(5000) // d1's bound; the endpoint fails its window and hands d2
    await w.line(submitLine('d2', 'same', N2))
    expect(w.submits).toEqual([`moxi-turns-window: ${N1}\nsame`, `moxi-turns-window: ${N2}\nsame`])
    pending[0]?.({ drop: 'moxi-turns-gate: a late prompt was dropped (late)' })
    await w.clock.settle()
    await turnStart($, 't2')
    pending[1]?.({ text: 'same' })
    await w.clock.settle()
    expect(closes(w)).toMatchObject([
      { delivery: 'd1', end: 'drop', reason: 'late' },
      { delivery: 'd2', end: 'resolve' },
    ])
    expect(bounds(w)).toMatchObject([{ delivery: 'd1' }])
  })

  test('the endpoint\'s end closes the window, and nothing more is sent', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.settle = () => new Promise(() => {})
    await w.line(submitLine('d1', 'a'))
    const before = w.got.length
    await w.end()
    await w.clock.advance(10_000)
    await turnStart($, 't9')
    expect(w.got.length).toBe(before)
    expect(w.stateWrites).toContainEqual({ key: 'endpoint', value: null })
  })
})

describe('the other lines', () => {
  test('live: the busy subagents and the box, answered on /live', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    w.agents = [{ id: 'a', status: 'running' }, { id: 'b', status: 'completed' }]
    w.box = 'half typed'
    await w.line({ op: 'live', ask: 7 })
    expect(w.got.find(g => g.path === '/live')?.body).toEqual({ ask: 7, agents: 1, draft: true })
  })

  test('live: an unreadable subagent list is sent as null, never 0', async ($: E, on) => {
    const w = world(on)
    w.agentsFail = true
    await attached($, w)
    await w.line({ op: 'live', ask: 8 })
    expect(w.got.find(g => g.path === '/live')?.body).toEqual({ ask: 8, agents: null, draft: false })
  })

  test('append: a steer is a user row, reported', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    await w.line({ op: 'append', delivery: 'd3', text: 'also this' })
    expect(w.kinds()).not.toContain('append.failed')
  })

  test('command: marked for the gate, run, and reported with the session id', async ($: E, on) => {
    const w = world(on)
    await attached($, w)
    await w.line({ op: 'command', request: 'r1', name: 'clear' })
    expect(w.commands).toEqual(['clear'])
    expect(w.stateWrites).toContainEqual({ key: 'command', value: { request: 'r1', name: 'clear', sock: SOCK } })
    expect(w.reports().find(r => r.kind === 'command.done')).toEqual({ seq: 2, kind: 'command.done', request: 'r1', state: 'ran', reason: null, new_session_id: 'S-1' })
  })

  test('command: with the gate not loaded nothing runs, and it reads unsupported', async ($: E, on) => {
    const w = world(on, { gate: false })
    await attached($, w)
    await w.line({ op: 'command', request: 'r1', name: 'exit' })
    expect(w.commands).toEqual([])
    expect(w.reports().find(r => r.kind === 'command.done')).toMatchObject({ request: 'r1', state: 'refused', reason: 'unsupported' })
  })

  for (const source of ['clear', 'resume', 'compact', 'startup']) {
    test(`a SessionStart (${source}) writes the mod.sock path again`, async ($: E, on) => {
      const w = world(on)
      await attached($, w)
      const n = w.stateWrites.length
      await $.classic.SessionStart({ source })
      expect(w.stateWrites.slice(n)).toContainEqual({ key: 'endpoint', value: { sock: SOCK } })
    })
  }
})

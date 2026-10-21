// The gate (design §5.4 step 4, §5.5, §5.7): the start's re-check and drop, the session
// command's re-check, and everything else passed untouched.

import { describe, expect, mock, test } from 'claude-code/testing'
import type { Plugin } from 'claude-code/testing'
import type { On } from 'claude-code'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type E = any

const SOCK = '/run/user/1000/moxi/turns/claude-n1/mod.sock'
const RUNNER_ORIGIN = { kind: 'plugin', name: 'moxi-turns' }

/** The runner's stand-in: it owns the runner's $.state, so the test sets the marks through it. */
const RUNNER: Plugin = {
  name: 'moxi-turns',
  register(on) {
    on('command.run', { command: 'runner-set' }, async ($, e) => {
      const v = JSON.parse(e.args) as Record<string, unknown>
      if ('endpoint' in v) await ($.state as E).set({ plugin: 'moxi-turns', key: 'endpoint' }, v.endpoint)
      if ('command' in v) await ($.state as E).set({ plugin: 'moxi-turns', key: 'command' }, v.command)
      return { text: 'set' }
    })
  },
}
const PLUGINS = { plugins: [RUNNER] }

type Answer = Record<string, unknown> | 'hang' | 'fail'

function world(on: On) {
  const clock = mock.clock(on, { now: 5_000 })
  const w = {
    clock,
    agents: [] as { id: string; status: string }[] | Error,
    box: '' as string | Error,
    got: [] as { path: string; body: Record<string, unknown> }[],
    answers: {} as Record<string, Answer>,
    entered: [] as string[],
    origins: [] as unknown[],
    contexts: [] as unknown[],
    ran: [] as string[],
    registered: [] as string[],
  }
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('command.register', async ($, e) => {
    w.registered.push(e.name)
    return { value: { command: e.name } } as never
  })
  on('command.describe', async ($, e) => ({ description: e.description, isHidden: e.isHidden }))
  on('agent.list', async () => {
    if (w.agents instanceof Error) throw w.agents
    return { value: w.agents as never }
  })
  on('prompt.read', async () => {
    if (w.box instanceof Error) throw w.box
    return { value: { text: w.box, cursor: w.box.length } }
  })
  on('http.fetch', async ($, e) => {
    const path = new URL(e.url).pathname
    w.got.push({ path, body: JSON.parse(String(e.init?.body ?? '{}')) })
    expect(e.init?.socketPath).toBe(SOCK)
    const a = w.answers[path] ?? { verdict: 'pass' }
    if (a === 'fail') throw new Error('ECONNREFUSED')
    if (a === 'hang') await new Promise(() => {})
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(a) } }
  })
  on('prompt.submit', async ($, e) => {
    w.entered.push(e.text)
    w.origins.push(e.origin)
    w.contexts.push(e.context)
    return { text: e.text, origin: e.origin }
  })
  on('command.run', async ($, e) => {
    w.ran.push(e.command)
    return { text: `engine ran ${e.command}` }
  })
  return Object.assign(w, {
    async boot($: E) {
      await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    },
    async mark($: E, values: Record<string, unknown>) {
      await $.command.run({ command: 'runner-set', args: JSON.stringify(values), origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    },
    submit($: E, text: string, origin: Record<string, unknown> = RUNNER_ORIGIN, context?: string[]) {
      return $.prompt.submit({ text, origin, wait: false, ...(context ? { context } : {}) })
    },
    run($: E, command: string, origin: Record<string, unknown> = RUNNER_ORIGIN) {
      return $.command.run({ command, args: '', origin, presentation: { isFullscreen: false, columns: 80 } })
    },
  })
}

const NONCE = '0123456789abcdef'.repeat(2)
const endpointMark = { endpoint: { sock: SOCK } }
const prompt = (text: string, nonce = NONCE) => `moxi-turns-window: ${nonce}\n${text}`
const GONE = 'moxi-turns-gate: a moxi-turns prompt was dropped (gone)'

describe('what the gate acts on', () => {
  test('it registers its hidden presence command, which runs nothing', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    expect(w.registered).toContain('moxi-turns-gate')
    expect((await w.run($, 'moxi-turns-gate', { kind: 'composer' })).text).toBe('moxi-turns-gate: loaded')
  })

  for (const origin of [{ kind: 'composer' }, { kind: 'sdk' }, { kind: 'plugin', name: 'someone-else' }]) {
    test(`a prompt from ${origin.kind} passes untouched, with no ask, nonce line and all`, PLUGINS, async ($: E, on) => {
      const w = world(on)
      await w.boot($)
      await w.mark($, endpointMark)
      w.box = 'typed'
      const r = await w.submit($, prompt('hi'), origin)
      expect(r.drop).toBeUndefined()
      expect(w.entered).toEqual([prompt('hi')])
      expect(w.got).toEqual([])
    })
  }

  test('the person\'s own /clear passes untouched', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    expect((await w.run($, 'clear', { kind: 'composer' })).text).toBe('engine ran clear')
    expect(w.got).toEqual([])
  })
})

describe('entry: every runner prompt is admitted by the endpoint, or dropped', () => {
  test('an admitted prompt enters without its nonce line, keeping its origin and context', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    const r = await w.submit($, prompt('mail\nline two'), RUNNER_ORIGIN, ['ctx'])
    expect(r.drop).toBeUndefined()
    expect(w.got).toEqual([{ path: '/gate/entry', body: { nonce: NONCE, draft: false, agents: 0 } }])
    expect(w.entered).toEqual(['mail\nline two'])
    expect(w.origins).toEqual([RUNNER_ORIGIN])
    expect(w.contexts).toEqual([['ctx']])
    expect(r.origin).toEqual(RUNNER_ORIGIN)
  })

  test('a reason from the endpoint drops it, never entered, with the endpoint\'s line', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    w.answers['/gate/entry'] = { verdict: 'drop', reason: 'stale-turn', text: 'moxi-turns-gate: the start was refused (stale-turn)' }
    const r = await w.submit($, prompt('go'))
    expect(r.drop).toBe('moxi-turns-gate: the start was refused (stale-turn)')
    expect(w.entered).toEqual([])
  })

  test('a late prompt (its window closed) is dropped, never entered (PB10)', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    w.answers['/gate/entry'] = { verdict: 'drop', reason: 'late', text: 'moxi-turns-gate: a late prompt was dropped (late)' }
    expect((await w.submit($, prompt('mail'))).drop).toBe('moxi-turns-gate: a late prompt was dropped (late)')
    expect(w.entered).toEqual([])
  })

  test('it hands the endpoint both readings; an unreadable box reads as a draft, an unreadable list as busy', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    w.box = 'half typed'
    w.agents = [{ id: 'a', status: 'waiting' }, { id: 'b', status: 'completed' }]
    await w.submit($, prompt('go'))
    expect(w.got[0]?.body).toEqual({ nonce: NONCE, draft: true, agents: 1 })
    w.box = new Error('unreadable')
    w.agents = new Error('no list')
    await w.submit($, prompt('go'))
    expect(w.got[1]?.body).toEqual({ nonce: NONCE, draft: true, agents: true })
  })

  test('a runner prompt with no nonce line is dropped gone, with no ask', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    expect((await w.submit($, 'mail with no nonce')).drop).toBe(GONE)
    expect((await w.submit($, `moxi-turns-window: 1234\nshort nonce`)).drop).toBe(GONE)
    expect(w.got).toEqual([])
    expect(w.entered).toEqual([])
  })

  test('with no mod.sock path in $.state (a /clear emptied it) it is dropped gone', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    expect((await w.submit($, prompt('mail'))).drop).toBe(GONE)
    await w.mark($, { endpoint: null })
    expect((await w.submit($, prompt('mail'))).drop).toBe(GONE)
    expect(w.got).toEqual([])
  })

  test('an endpoint that does not answer within 1 s drops it as gone', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    w.answers['/gate/entry'] = 'hang'
    const r = w.submit($, prompt('go'))
    await w.clock.advance(1000)
    expect((await r).drop).toBe(GONE)
    expect(w.entered).toEqual([])
  })

  test('an endpoint that refuses the connection drops it as gone', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, endpointMark)
    w.answers['/gate/entry'] = 'fail'
    expect((await w.submit($, prompt('go'))).drop).toBe(GONE)
  })
})

describe('a session command\'s re-check', () => {
  const commandMark = (name = 'clear') => ({ command: { request: 'r1', name, sock: SOCK } })

  for (const name of ['clear', 'exit']) {
    test(`/${name} the runner queued runs when the endpoint passes it`, PLUGINS, async ($: E, on) => {
      const w = world(on)
      await w.boot($)
      await w.mark($, commandMark(name))
      expect((await w.run($, name)).text).toBe(`engine ran ${name}`)
      expect(w.got).toEqual([{ path: '/gate/command', body: { request: 'r1', draft: false, agents: 0 } }])
    })
  }

  test('a reason from the endpoint withdraws the run', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, commandMark())
    w.answers['/gate/command'] = { verdict: 'drop', reason: 'busy-turn', text: 'moxi-turns-gate: /clear was refused (busy-turn)' }
    expect((await w.run($, 'clear')).text).toBe('moxi-turns-gate: /clear was refused (busy-turn)')
    expect(w.ran.filter(c => c === 'clear')).toEqual([])
  })

  test('a command always hands the endpoint both readings', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, commandMark())
    w.box = new Error('no box')
    w.agents = [{ id: 'a', status: 'pending' }]
    await w.run($, 'clear')
    expect(w.got[0]?.body).toEqual({ request: 'r1', draft: true, agents: 1 })
  })

  test('a run of the runner with no mark, or another command\'s mark, is withdrawn', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    expect((await w.run($, 'exit')).text).toBe('moxi-turns-gate: /exit was refused (no-pending)')
    await w.mark($, commandMark('clear'))
    expect((await w.run($, 'exit')).text).toBe('moxi-turns-gate: /exit was refused (no-pending)')
    expect(w.got).toEqual([])
  })

  test('no answer within 1 s withdraws it as gone', PLUGINS, async ($: E, on) => {
    const w = world(on)
    await w.boot($)
    await w.mark($, commandMark('exit'))
    w.answers['/gate/command'] = 'hang'
    const r = w.run($, 'exit')
    await w.clock.advance(1000)
    expect((await r).text).toBe('moxi-turns-gate: /exit was refused (gone)')
  })
})

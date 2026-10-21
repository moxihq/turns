// The runner's contract: the two values it keeps in $.state, written by the runner alone
// and read by the gate (moxi-turns-gate). A /clear empties $.state; the runner writes the
// endpoint's path again at every SessionStart and before each submit.

/** Where the gate asks: the endpoint's mod.sock, or null with no endpoint. A runner prompt
 *  carries its window nonce on its first line; the gate asks the endpoint with it. */
export type MoxiTurnsEndpoint = {
  sock: string
}

/** The session command the runner queued (one at most), or null. */
export type MoxiTurnsCommand = {
  /** The request id the endpoint minted. */
  request: string
  name: 'clear' | 'exit'
  sock: string
}

declare module 'claude-code' {
  interface PluginState {
    'moxi-turns': {
      endpoint: MoxiTurnsEndpoint | null
      command: MoxiTurnsCommand | null
    }
  }
}

# turns' socket protocol, version 1

This is the contract between turns' endpoints and every caller. `moxi_turns` speaks it for
its callers; anything that speaks it the same way is a caller too. The Claude runner's leg
to its own endpoint (JSON lines on the child's stdout, HTTP on `mod.sock`) is internal to
the package and is not part of this contract.

## Finding an endpoint

Each session turns can reach has one endpoint: a small turns process with one socket.

- Its directory is `RUNTIME/turns/<harness>-<nonce>/`, where `RUNTIME` is Moxi's runtime
  root (`$XDG_RUNTIME_DIR/moxi`, moved by `MOXI_TURNS_RUNTIME_DIR` or `MOXI_TURNS_HOME`;
  see `moxi.paths`). The nonce is minted when the endpoint starts, so the key is the
  launch, not the session id: a `/clear` keeps the endpoint and `hello.json` follows the
  new id.
- The directory is 0700 and holds `sock` (0600) and `hello.json`. A Claude endpoint also
  holds `mod.sock`, which no caller uses.
- `hello.json` is written after `sock` listens, and rewritten when the session id changes.
  It holds the same object the `hello` op answers (below).
- A directory counts only if its socket answers `hello`. A caller that finds a socket
  refusing (ECONNREFUSED or ENOENT, and nothing else) may sweep the directory: remove
  `hello.json`, `sock`, `mod.sock` and `.hello.json.tmp`, then the directory if it is empty. A directory with
  no readable `hello.json` is an endpoint starting, and is left alone.
- By session id: an endpoint matches when its current `session_id` is the id, or else when
  `earlier` holds it. By process id: walk up from the pid through its parents; the nearest
  ancestor that is an endpoint's `pid` matches.

Any process running as the user can reach any endpoint; turns adds nothing to that trust
model.

## Framing

- One JSON object per line, UTF-8, both ways. A line is at most 4 MiB.
- Every request carries `"v": 1` and `"op"`. A request that wants an answer carries an
  `"id"`, which the answer echoes.
- An answer is `{"v": 1, "id": …, "ok": true, …}` or
  `{"v": 1, "id": …, "ok": false, "code": …, "error": "<words>"}`. Codes: `version` (the
  answer also carries `"versions"`, the list the endpoint speaks), `unknown-op`,
  `bad-request`, `held`, `gone`.
- A line that is not a JSON object is answered `bad-request` and the connection ends.
- A connection may carry several requests, one after another. `watch` and `hold` keep the
  connection for themselves.

## Values

Every result and event is an object with a `"kind"`:

| kind | fields |
|---|---|
| `facts` | `session_id`, `epoch`, `turn_open`, `agents`, `draft`, `at`, `stale` |
| `started` | `turn_id` |
| `refused` | `reason` |
| `lost` | `reason` |
| `ended` | `turn_id`, `outcome` |
| `ran` | `new_session_id` (the new id after `/clear`, else null) |
| `failed` | `reason` |
| `passed` | none: an `/exit` the harness's gate let through (see `command`) |
| `ready` | `source`, `session_id` |
| `turn-started` | `turn_id`, `session_id`, `delivery_id` (null for the person's own turn) |
| `turn-ended` | `turn_id`, `outcome`, `session_id`, `delivery_id`, `steered` (list of delivery ids) |
| `gone` | `reason` |
| `stop` | `turn_id`, `session_id`, `stop_hook_active`, `task_types`, `crons` (each null when the harness did not give it) |

- `epoch` is the current session's count of main-loop turns started; a new session id
  starts it again at 0. `agents` counts subagents pending, running or waiting, and is
  null when they could not be listed (unknown, which counts as busy: a start with
  `refuse_agents` and a command refuse `busy-agents`). `draft` is true when the person has
  typed into the prompt box, and true when the box could not be read. `stale` is true when the turn ledger moved while the live facts were read.
- `outcome` is one of `answer`, `aborted` (Esc), `error`, `refusal`.
- A delivery's refusal `reason` is one of `busy-turn`, `stale-turn`, `draft`,
  `busy-agents`, `gone`, `unsupported`, `dropped` (another plugin's hook dropped the
  prompt), `idle` (a steer with no turn running), and `loading` (a Codex host has no
  accepted load).
- A `lost` `reason` that starts with `untied` or `expired` was decided by an endpoint
  that lives on: the delivery's submit closed with no turn tied to it (on Claude Code, a
  resolve with no turn in its window, a rejected submit, or the runner's bound; on Codex,
  a request Codex was sent that the host could not tie to a turn), or the harness neither
  started nor refused it in time. Any other `lost` means the endpoint
  went away (a reload, a crash, the session ended). A caller tells the two apart by the
  prefix: the first settles one delivery, the second means looking for the endpoint
  again.
- On Claude Code a delivery whose submit failed before the endpoint admitted its prompt
  never runs: the prompt is dropped where it would enter. So a caller that hands the text
  again after such a `lost` delivers it once.
- On a Codex host a turn ends only on Codex's own word: its `turn/completed` (whose
  status gives the outcome), a change of the thread to a non-active status with no
  `turn/completed` for the open turn within a short grace (the turn ends `error`), a load
  whose answer shows the turn gone, or a dead app-server. The Stop and Interrupt hooks
  close nothing: between Stop and `turn/completed` the turn is still open. Once a Stop
  is answered without a block the turn is ending: Codex may complete it at any moment and
  can leave a text it takes then unanswered, so it takes no more steers. A `deliver` with
  `steer` waits and starts a new turn after `turn/completed`, and a `steer` is refused
  `idle`, as with no turn running. A delivery reads `refused` only when Codex did not
  take its text (the request was never sent, or Codex answered it with an error). A
  `turn/start` or `turn/steer` Codex was sent but that the host cannot tie to a turn (no
  answer came, or Codex took the text into a running turn the host had already reported
  or could not name) reads `lost` at once, with a reason starting `untied`.
- On an OpenCode host (experimental) a delivery's turn is the one whose reply names the
  host's posted message as its parent. A turn that goes idle with no such reply is the
  person's, and the delivery reads `lost` with a reason starting `untied`.
- A command's refusal `reason` is one of `busy-turn`, `stale-turn`, `draft`,
  `busy-agents`, `gone`, `unsupported`, `not-allowed` (no consent), `in-flight` (one is
  already running), `rate` (one was accepted in the last 60 s).
- `source` is one of `startup`, `resume`, `clear`, `compact`, or null when the harness did
  not say.

## Ops

### `hello`

Answer: `"hello": {harness, pid, session_id, earlier, cwd, can, nonce, started,
endpoint_version, mod_version, protocol, trouble?}`. `harness` is `claude`, `codex` or `opencode`;
`pid` the harness process; `earlier` the earlier session ids of the same launch; `can` a
list drawn from `start`, `steer`, `hold`, `clear`, `exit`; `protocol` the versions spoken.
`trouble` is absent when the endpoint is healthy and otherwise gives the host's current
load warning or failure.

### `facts`

Answer: `"facts": <facts>`. The endpoint reads its ledger, then the live facts, then the
ledger again.

### `start` — `text`, `if_epoch` (int or null), `refuse_draft`, `refuse_agents` (booleans)

A new turn, only if no turn is open. With `if_epoch`, only if the epoch still equals it.
`refuse_draft` refuses on a draft in the prompt box, `refuse_agents` while a subagent is
live. A delivery already handed to the harness or held for the running turn counts as an
open turn. The same checks run again where the start takes effect (on Claude Code, as the
prompt enters; on an OpenCode host, as the host reads the session's status just before
it posts), so a turn that began after the caller's read refuses the start.
A Codex host with no accepted load refuses `loading`.

Answer: `"delivery": "<id>"`, and `"result": <refused>` when it was refused at once.

### `deliver` — `text`, `steer` (boolean)

A new turn now if the session is idle, else when the running turn ends. Deliveries are not
coalesced: two are two turns, in the order handed. With `steer` true on a session that
`can` steer, a running turn takes the text at once (as `steer` below) and an idle session
gets a new turn.
On a Codex host with no accepted load, the delivery waits until a load is accepted,
including when `steer` is true.

Answer: as `start`.

### `steer` — `text`

The text into the running turn. Refused `idle` when no turn runs, `unsupported` where the
session cannot steer, and `loading` when a Codex host has no accepted load. A steered
delivery's `started` names the running turn, and its `ended` is that turn's end.

Answer: as `start`.

### `wait` — `delivery`, `until` (`started` or `ended`), `timeout_ms` (number or null)

Waits for the delivery's result. Until `started`: `started`, `refused` or `lost`. Until
`ended`: `ended`, `refused` (the delivery never went in) or `lost`. A delivery id this
endpoint did not mint, or one settled long ago, answers `lost`. A delivery handed to the
harness that the harness neither starts nor refuses within 300 s answers `lost` with a
reason starting `expired`, and the next held delivery goes in.

Answer: `"result": <value>`, or `"pending": true` when `timeout_ms` passed first. With
`timeout_ms` null the endpoint waits as long as it lives.

### `command` — `name` (`clear` or `exit`), `if_epoch` (int or null), `timeout_ms`

Runs `/clear` or `/exit`. Only with consent: the user's turns config `commands.allow`, or
the launch's `MOXI_TURNS_COMMANDS` (a comma-separated list) in the session's environment.
Only one in flight, one accepted per 60 s, and only from an idle snapshot: no open turn,
no live subagent, no draft. The checks of the ledger run again where the command takes
effect.

Answer: `"result": <ran | refused | failed | passed>`. `timeout_ms` (default 30000)
bounds the wait for the harness; past it the answer is `failed`, and the command may
still run.

`/exit` cannot be confirmed by the endpoint, which the harness ends before or as it exits.
When the harness's gate lets an `/exit` through, the endpoint answers `passed` at once (or
at its own end, if that comes first). The caller then watches the harness process that
`hello` names (a pidfd on Linux, opened before the request) and reads the exit as run once
that process has ended, or as failed (`not-exited`) when it still runs after 5 s.
`moxi_turns` does this and answers `Ran` or `Failed("not-exited")`; a caller never sees
`passed` from the library. A
command the harness does not settle within 300 s settles `failed` with a reason starting
`expired`, which frees the slot for the next one (the 60 s rate still applies).

### `watch`

Answer: `{"ok": true}`, then one frame `{"v": 1, "event": <value>}` per event, in the order
the ledger saw them: `ready` at a start, resume, clear or compaction; `turn-started` and
`turn-ended` for every main-loop turn, the person's and the deliveries' alike; and one
`gone` when the endpoint ends, after which the connection closes. A caller hangs up to stop
watching. A connection that ends without `gone` means the endpoint went away.

### `hold`

Makes this connection the session's one holder. Answer: `{"ok": true}`, or `held` when
another connection holds it. At each Stop of the main loop the endpoint sends
`{"v": 1, "op": "stop", "ask": <n>, "facts": <stop>}`, and the holder answers on the same
connection with `{"v": 1, "op": "stop-answer", "ask": <n>, "reason": "<text>" | null}`. A
reason holds the turn open with that text; null lets it end. An answer that does not come
within `hold.wait_ms` (turns config, default 2000) lets the turn end, and a late answer is
ignored. Closing the connection gives the hold up.

## Versions

This is version 1. A request in a version the endpoint does not speak is answered
`version` with the list it does. A change to any frame's shape raises the version; an
endpoint may speak several. `hello.json` and `hello` name the versions spoken and the
endpoint's package version.

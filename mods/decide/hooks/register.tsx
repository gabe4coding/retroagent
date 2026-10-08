// retroagent-decide: a band above the prompt when questions of `kb decide` wait for the user, and a pane to answer
// them. The questions are retro suggestions (accept: new sessions of the repo see the change) and memory fixes the
// pages routine proposed (accept: kb changes this machine's memory file). See src/kb/decide.py in retroagent.
//
// The mod runs kb and keeps no logic of its own: `kb decide --json` lists the questions and the day the user hid
// them until (`kb decide later`), and each button runs `kb decide accept|reject <id> --yes`. A button press is the
// user's own act; an agent's Bash call to `kb decide accept|reject` is blocked by retroagent's PreToolUse hook.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Item } from '../types'

const PANE = 'retroagent-decide'
const DETAIL_LINES = 12
const TIMEOUT_MS = 60_000

const items = atom({ plugin: 'retroagent-decide', key: 'items' } as const, [] as Item[])
const hiddenUntil = atom({ plugin: 'retroagent-decide', key: 'hiddenUntil' } as const, '')
const message = atom({ plugin: 'retroagent-decide', key: 'message' } as const, '')

type Ran = { exitCode: number; stdout: string; stderr: string }

function asItems(value: unknown): Item[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(row => {
    if (!row || typeof row !== 'object') return []
    const r = row as Record<string, unknown>
    if (typeof r.id !== 'string' || (r.kind !== 'suggestion' && r.kind !== 'memory')) return []
    const sources = Array.isArray(r.sources) ? r.sources.filter((s): s is string => typeof s === 'string') : []
    return [{ id: r.id, kind: r.kind, title: String(r.title ?? ''), detail: String(r.detail ?? ''), sources }]
  })
}

function today(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// kb as configured; when it does not start (not on the PATH of the app), the link install.sh makes
async function kb($: EngineInterface, command: string, args: string[]): Promise<Ran> {
  try {
    return await $.process.run([command, ...args], { timeoutMs: TIMEOUT_MS })
  } catch (first) {
    const home = await $.env.get('HOME')
    if (!home) throw first
    return await $.process.run([`${home}/.local/bin/kb`, ...args], { timeoutMs: TIMEOUT_MS })
  }
}

async function refresh($: EngineInterface, command: string): Promise<void> {
  let found: Item[] = []
  let until = ''
  try {
    const ran = await kb($, command, ['decide', '--json'])
    if (ran.exitCode === 0) {
      const data = JSON.parse(ran.stdout) as { items?: unknown; hidden_until?: unknown }
      found = asItems(data.items)
      until = typeof data.hidden_until === 'string' ? data.hidden_until : ''
    }
  } catch {
    found = []                              // no kb, or no data repo yet: nothing to show
  }
  await update($, items, () => found)
  await update($, hiddenUntil, () => until)
}

// run kb, show what it said in the pane, then list the questions again
async function answer($: EngineInterface, command: string, args: string[]): Promise<void> {
  let said: string
  try {
    const ran = await kb($, command, args)
    said = `${ran.stdout}\n${ran.stderr}`.trim().split('\n').filter(Boolean).join(' · ')
  } catch (e) {
    said = `kb did not start: ${e instanceof Error ? e.message : String(e)}`
  }
  await update($, message, () => said)
  await refresh($, command)
}

export const register: Register = (on, options) => {
  const command = typeof options.kb === 'string' && options.kb.trim() ? options.kb.trim() : 'kb'

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'decide', description: 'Answer the retro suggestions and memory fixes that wait' })
    await refresh($, command)
    return next(e)
  })

  // /clear, /resume and /branch reset $.state, and session.start does not fire again
  on('classic.SessionStart', { source: ['clear', 'resume', 'fork'] }, async ($, e, next) => {
    await refresh($, command)
    return next(e)
  }).catch(($, e, next) => next(e))         // a failed refresh never holds up the session

  on('command.run', { command: 'decide' }, async $ => {
    await refresh($, command)
    const list = await read($, items)
    if (list.length === 0) return { text: 'Nothing waits for you.' }
    await $.ui.open({ id: PANE, title: 'kb decide', focus: true, closeOnEscape: true })
    return {}
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, items)
    const until = await read($, hiddenUntil)
    if (e.props.hasSurvey || list.length === 0 || (until && today() < until)) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const theirs = await next(e)
    const n = list.length
    return (
      <Box flexDirection="column">
        {theirs}
        <Box flexDirection="row" columnGap={1}>
          <Text color="warning">retroagent:</Text>
          <Text>
            {n} question{n === 1 ? '' : 's'} wait{n === 1 ? 's' : ''} for your answer
          </Text>
          <Button
            key="review"
            label="Review"
            onPress={() => $.ui.open({ id: PANE, title: 'kb decide', focus: true, closeOnEscape: true })}
          />
          <Button key="later" label="Later" onPress={() => answer($, command, ['decide', 'later'])} />
        </Box>
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = await read($, items)
    const said = await read($, message)
    return (
      <Box flexDirection="column" rowGap={1}>
        {said ? <Text dimColor>{said}</Text> : null}
        {list.length === 0 ? <Text>Nothing waits for you.</Text> : null}
        {list.map(item => {
          const lines = item.detail.split('\n')
          const shown = lines.slice(0, DETAIL_LINES).join('\n')
          return (
            <Box flexDirection="column">
              <Text bold>
                {item.id} · {item.kind === 'memory' ? 'memory fix' : 'retro suggestion'}
              </Text>
              <Text>{item.title}</Text>
              {shown ? <Text dimColor>{shown}</Text> : null}
              {lines.length > DETAIL_LINES ? (
                <Text dimColor>… {lines.length - DETAIL_LINES} more lines: kb decide</Text>
              ) : null}
              {item.sources.length > 0 ? <Text dimColor>sources: {item.sources.join(', ')}</Text> : null}
              <Box flexDirection="row" columnGap={2}>
                <Button
                  key={`accept-${item.id}`}
                  label="Accept"
                  onPress={() => answer($, command, ['decide', 'accept', item.id, '--yes'])}
                />
                <Button
                  key={`reject-${item.id}`}
                  label="Reject"
                  onPress={() => answer($, command, ['decide', 'reject', item.id, '--yes'])}
                />
              </Box>
            </Box>
          )
        })}
        <Box flexDirection="row" columnGap={2}>
          <Button key="pane-later" label="Later" onPress={() => answer($, command, ['decide', 'later'])} />
          <Button key="close" label="Close" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )
  })
}


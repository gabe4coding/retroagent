import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SUGGESTION = {
  id: 's-1a2b3c', kind: 'suggestion', title: 'Rules · wait for CI with a watch command',
  detail: 'still happening: 3 sessions in 14 days', sources: ['a1b2c3d4'],
}
const FIX = {
  id: 'm-4d5e6f', kind: 'memory', title: 'delete memory demo/old: a later session replaced it',
  detail: 'the old text', sources: ['b1b2c3d4'],
}
const SURFACES = ['terminal', 'desktop'] as const
const BAND = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 },
               view: {} }
const DECIDE = { command: 'decide', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false,
                 columns: 100 } } as const

// a fake kb: `decide --json` lists what is left in `start`, and each answer removes its question; session.id answers
// `session.id`
function fakeKb(on: On, start: object[], hiddenUntil = '', session = { id: 'session-1' }) {
  const ran: string[][] = []
  const left = start
  on('session.id', () => ({ value: session.id }))
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    ran.push(argv)
    const [, , verb, id] = argv
    if (verb === '--json') {
      return { value: { exitCode: 0, stdout: JSON.stringify({ items: left, hidden_until: hiddenUntil }), stderr: '',
                        isStdoutTruncated: false, isStderrTruncated: false } }
    }
    left.splice(left.findIndex(item => (item as { id: string }).id === id), 1)
    return { value: { exitCode: 0, stdout: `${id}: ${verb}ed. The next sync pushes it.\n`, stderr: '',
                      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('ui.render', () => ({ type: 'Box', props: {}, children: [] }))     // the engine's own band: empty
  return ran
}

for (const surface of SURFACES) {
  test(`the band shows the waiting questions and the pane answers them (${surface})`, async ($, on) => {
    const ran = fakeKb(on, [SUGGESTION, FIX])
    await $.command.run(DECIDE)
    const band = await $.ui.mount({ plugin: 'retroagent', surface, component: 'AbovePrompt', props: BAND })
    expect((await band.find({ text: /2 questions wait/ }))).toBeDefined()
    expect((await band.find({ key: 'review' }))?.props.label).toBe('Review')

    const pane = await $.ui.mount({ plugin: 'retroagent', surface, component: 'Pane',
                                    requestId: 'retroagent-decide', props: {} as never })
    expect(await pane.find({ text: /retro suggestion/ })).toBeDefined()
    expect(await pane.find({ text: /memory fix/ })).toBeDefined()
    await pane.press({ key: 'accept-m-4d5e6f' })
    expect(ran).toContainEqual([expect.stringMatching(/\/bin\/kb$/), 'decide', 'accept', 'm-4d5e6f', '--yes'])
    expect(await pane.find({ text: /m-4d5e6f: accepted/ })).toBeDefined()
    expect(await pane.find({ key: 'accept-m-4d5e6f' })).toBeUndefined()

    await band.redraw()
    expect(await band.find({ text: /1 question waits/ })).toBeDefined()
  })
}

test('the band stays empty when nothing waits or the user chose later', async ($, on) => {
  fakeKb(on, [SUGGESTION], '2999-01-01')
  await $.command.run(DECIDE)
  const band = await $.ui.mount({ plugin: 'retroagent', surface: 'terminal', component: 'AbovePrompt',
                                  props: BAND })
  expect(await band.find({ key: 'review' })).toBeUndefined()
})

test('the command says so when nothing waits', async ($, on) => {
  fakeKb(on, [])
  const out = await $.command.run(DECIDE)
  expect(out).toMatchObject({ text: 'Nothing waits for you.' })
})

test('the band lists the questions again in a new session after /clear, once', async ($, on) => {
  const clock = mock.clock(on)
  const session = { id: 'cleared-1' }
  const waiting: object[] = [SUGGESTION]
  const ran = fakeKb(on, waiting, '', session)
  const lists = () => ran.filter(argv => argv[2] === '--json').length
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  expect(lists()).toBe(1)
  await clock.advance(10_000)
  expect(lists()).toBe(1)

  waiting.push(FIX)
  session.id = 'cleared-2'                  // what /clear, /resume and /branch do
  await clock.advance(2_000)
  expect(lists()).toBe(2)
  const band = await $.ui.mount({ plugin: 'retroagent', surface: 'terminal', component: 'AbovePrompt',
                                  props: BAND })
  expect(await band.find({ text: /2 questions wait/ })).toBeDefined()
  await clock.advance(10_000)
  expect(lists()).toBe(2)
})

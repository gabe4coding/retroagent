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

// a fake kb: `decide --json` lists what is left, and each answer removes its question
function fakeKb(on: On, start: object[], hiddenUntil = '') {
  const ran: string[][] = []
  let left = [...start]
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    ran.push(argv)
    if (argv[0] !== 'kb') return { deny: 'not found' }
    const [, , verb, id] = argv
    if (verb === '--json') {
      return { value: { exitCode: 0, stdout: JSON.stringify({ items: left, hidden_until: hiddenUntil }), stderr: '',
                        isStdoutTruncated: false, isStderrTruncated: false } }
    }
    left = left.filter(item => (item as { id: string }).id !== id)
    return { value: { exitCode: 0, stdout: `${id}: ${verb}ed. The next sync pushes it.\n`, stderr: '',
                      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('ui.render', () => ({ type: 'Box', props: {}, children: [] }))     // the engine's own band: empty
  mock.env(on, { HOME: '/home/me' })
  return ran
}

for (const surface of SURFACES) {
  test(`the band shows the waiting questions and the pane answers them (${surface})`, async ($, on) => {
    const ran = fakeKb(on, [SUGGESTION, FIX])
    await $.command.run(DECIDE)
    const band = await $.ui.mount({ plugin: 'retroagent-decide', surface, component: 'AbovePrompt', props: BAND })
    expect((await band.find({ text: /2 questions wait/ }))).toBeDefined()
    expect((await band.find({ key: 'review' }))?.props.label).toBe('Review')

    const pane = await $.ui.mount({ plugin: 'retroagent-decide', surface, component: 'Pane',
                                    requestId: 'retroagent-decide', props: {} as never })
    expect(await pane.find({ text: /retro suggestion/ })).toBeDefined()
    expect(await pane.find({ text: /memory fix/ })).toBeDefined()
    await pane.press({ key: 'accept-m-4d5e6f' })
    expect(ran).toContainEqual(['kb', 'decide', 'accept', 'm-4d5e6f', '--yes'])
    expect(await pane.find({ text: /m-4d5e6f: accepted/ })).toBeDefined()
    expect(await pane.find({ key: 'accept-m-4d5e6f' })).toBeUndefined()

    await band.redraw()
    expect(await band.find({ text: /1 question waits/ })).toBeDefined()
  })
}

test('the band stays empty when nothing waits or the user chose later', async ($, on) => {
  fakeKb(on, [SUGGESTION], '2999-01-01')
  await $.command.run(DECIDE)
  const band = await $.ui.mount({ plugin: 'retroagent-decide', surface: 'terminal', component: 'AbovePrompt',
                                  props: BAND })
  expect(await band.find({ key: 'review' })).toBeUndefined()
})

test('the command says so when nothing waits', async ($, on) => {
  fakeKb(on, [])
  const out = await $.command.run(DECIDE)
  expect(out).toMatchObject({ text: 'Nothing waits for you.' })
})

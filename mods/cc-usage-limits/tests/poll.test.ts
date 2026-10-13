import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

const CLI = 'C:\\Projects\\cc-usage\\cc_usage\\cli.py'
const START = { cwd: 'C:\\work', surface: null, isInteractive: false }

type Run = { argv: readonly string[]; timeoutMs?: number; env?: Record<string, string> }

// Низ цепочки: session.start, лог и $.process.run, который пишет вызовы.
function base(on: On, fail = false): { runs: Run[]; logs: string[] } {
  const runs: Run[] = []
  const logs: string[] = []
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.log', ($, e) => {
    logs.push(String((e as { text?: string }).text))
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv, timeoutMs: e.init?.timeoutMs, env: e.init?.env })
    if (fail) throw new Error('python не найден')
    return {
      value: {
        exitCode: 0,
        stdout: 'poll-limits: замер сохранён\n',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  return { runs, logs }
}

test('настроен — первый запуск через 2 с, дальше раз в 10 минут', { options: { cli_path: CLI } }, async ($, on) => {
  const { runs, logs } = base(on)
  const clock = mock.clock(on, { now: 1_760_000_000_000 })
  await $.session.start(START)
  expect(runs.length).toBe(0)

  await clock.advance(2_000)
  expect(runs.length).toBe(1)
  expect(runs[0]?.argv).toEqual(['python', CLI, 'poll-limits', '--min-interval', '540'])
  expect(runs[0]?.timeoutMs).toBe(60_000)
  expect(runs[0]?.env).toEqual({ PYTHONUTF8: '1' })
  expect(logs.some(l => l.includes('замер сохранён'))).toBe(true)

  await clock.advance(10 * 60 * 1000 - 2_000)
  expect(runs.length).toBe(2)
  await clock.advance(10 * 60 * 1000)
  expect(runs.length).toBe(3)
})

test('свой интерпретатор из настроек', { options: { cli_path: CLI, python: 'C:\\Python313\\python.exe' } }, async ($, on) => {
  const { runs } = base(on)
  const clock = mock.clock(on, { now: 0 })
  await $.session.start(START)
  await clock.advance(2_000)
  expect(runs[0]?.argv[0]).toBe('C:\\Python313\\python.exe')
})

test('сбой запуска не бросает и не останавливает таймер', { options: { cli_path: CLI } }, async ($, on) => {
  const { runs, logs } = base(on, true)
  const clock = mock.clock(on, { now: 0 })
  const r = await $.session.start(START)
  expect(r.cwd).toBe(START.cwd)
  await clock.advance(2_000)
  expect(logs.some(l => l.includes('poll-limits не запустился'))).toBe(true)
  await clock.advance(10 * 60 * 1000)
  expect(runs.length).toBe(2)
})

test('не настроен — ничего не запускает', async ($, on) => {
  const { runs } = base(on)
  const clock = mock.clock(on, { now: 0 })
  const r = await $.session.start(START)
  expect(r.cwd).toBe(START.cwd)
  await clock.advance(30 * 60 * 1000)
  expect(runs.length).toBe(0)
})

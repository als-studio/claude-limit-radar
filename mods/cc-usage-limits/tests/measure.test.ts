import { test, expect, mock } from 'claude-code/testing'
import type { On, SessionRateLimit } from 'claude-code'

const HOME = 'C:\\Users\\tester'
// Движок нормализует разделители пути под ОС: сравниваем с прямыми слэшами.
const TAIL = 'C:/Users/tester/.claude/cc-usage/limit-samples/sess-1.ndjson'
const norm = (p: string) => p.replace(/\\/g, '/')

const logs: string[] = []

// Низ цепочки: id сессии, лог, окружение и сам session.measure.
function base(on: On): void {
  logs.length = 0
  on('session.id', () => ({ value: 'sess-1' }))
  on('ui.log', ($, e) => {
    logs.push(String((e as { text?: string }).text))
    return { value: undefined }
  })
  on('session.measure', ($, e) => ({ changed: e.changed }))
  mock.env(on, { USERPROFILE: HOME })
}

// Файловая система в памяти под плагином.
function memFs(on: On): Map<string, string> {
  const files = new Map<string, string>()
  on('fs.exists', ($, e) => ({ value: files.has(e.path) }))
  on('fs.read', ($, e) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error(`нет файла ${e.path}`)
    return { value: text }
  })
  on('fs.write', ($, e) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  base(on)
  return files
}

const LIMITS: SessionRateLimit[] = [
  { kind: 'five_hour', percentUsed: 23.5, resetsAt: '2026-10-12T15:00:00Z' },
  { kind: 'seven_day', percentUsed: 7, resetsAt: '2026-10-15T00:00:00.000Z' },
  { kind: 'spend_limit', percentUsed: 1 },
]

const measure = (rateLimits: SessionRateLimit[]) => ({
  context: { window: 200000 },
  rateLimits,
  changed: ['rateLimits' as const],
})

const lines = (files: Map<string, string>) =>
  ([...files].find(([k]) => norm(k) === TAIL)?.[1] ?? '').split('\n').filter(l => l !== '').map(l => JSON.parse(l))

test('пустой rateLimits — ничего не пишет', async ($, on) => {
  const files = memFs(on)
  mock.clock(on, { now: 1_760_000_000_000 })
  await $.session.measure(measure([]))
  expect(files.size).toBe(0)
})

test('обычный замер — строка в формате statusline, ISO → unix', async ($, on) => {
  const files = memFs(on)
  mock.clock(on, { now: 1_760_000_000_500 })
  await $.session.measure(measure(LIMITS))
  expect(logs).toEqual([])
  expect([...files.keys()].map(norm)).toEqual([TAIL])
  expect(lines(files)).toEqual([
    {
      ts: 1_760_000_000.5,
      session: 'sess-1',
      five_hour_pct: 23.5,
      five_hour_reset: Date.UTC(2026, 9, 12, 15) / 1000,
      seven_day_pct: 7,
      seven_day_reset: Date.UTC(2026, 9, 15) / 1000,
      fresh: 1,
      source: 'mod',
    },
  ])
})

test('нет окна — null', async ($, on) => {
  const files = memFs(on)
  mock.clock(on, { now: 1_760_000_000_000 })
  await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 40 }]))
  const [row] = lines(files)
  expect(row.five_hour_pct).toBe(null)
  expect(row.five_hour_reset).toBe(null)
  expect(row.seven_day_pct).toBe(40)
  expect(row.seven_day_reset).toBe(null)
})

test('повтор тех же цифр в пределах 60 с не пишется, после 60 с и при изменении — пишется', async ($, on) => {
  const files = memFs(on)
  const clock = mock.clock(on, { now: 1_760_000_000_000 })
  await $.session.measure(measure(LIMITS))
  await clock.advance(30_000)
  await $.session.measure(measure(LIMITS))
  expect(lines(files).length).toBe(1)

  await clock.advance(31_000)
  await $.session.measure(measure(LIMITS))
  expect(lines(files).length).toBe(2)

  await clock.advance(1_000)
  const moved = LIMITS.map(l => (l.kind === 'five_hour' ? { ...l, percentUsed: 24 } : l))
  await $.session.measure(measure(moved))
  expect(lines(files).length).toBe(3)
  expect(lines(files)[2].five_hour_pct).toBe(24)
})

test('сбой fs не ломает цепочку', async ($, on) => {
  on('fs.exists', () => {
    throw new Error('диск недоступен')
  })
  base(on)
  mock.clock(on, { now: 1_760_000_000_000 })
  const r = await $.session.measure(measure(LIMITS))
  expect(r.changed).toEqual(['rateLimits'])
  expect(logs.some(l => l.includes('замер не записан'))).toBe(true)
})

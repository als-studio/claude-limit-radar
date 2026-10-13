import type { EngineInterface, Register, SessionRateLimit } from 'claude-code'

// Повтор тех же цифр раньше этого срока не пишем.
const MIN_INTERVAL_S = 60

// Опрос лимита аккаунта: такт таймера, задержка первого запуска, таймаут процесса.
const POLL_EVERY_MS = 10 * 60 * 1000
const POLL_FIRST_MS = 2_000
const POLL_TIMEOUT_MS = 60_000
// Гейт poll-limits: параллельные сессии не дёргают эндпоинт чаще раза в 9 минут.
const POLL_MIN_INTERVAL_S = '540'

type Sample = {
  ts: number
  session: string
  five_hour_pct: number | null
  five_hour_reset: number | null
  seven_day_pct: number | null
  seven_day_reset: number | null
  fresh: 1
  source: 'mod'
}

// ISO 8601 → unix-секунды (int); нет даты или не разобралась → null.
const toUnix = (iso: string | undefined): number | null => {
  if (!iso) return null
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}

const isSame = (a: Partial<Sample>, b: Sample): boolean =>
  a.five_hour_pct === b.five_hour_pct &&
  a.five_hour_reset === b.five_hour_reset &&
  a.seven_day_pct === b.seven_day_pct &&
  a.seven_day_reset === b.seven_day_reset

async function record($: EngineInterface, limits: readonly SessionRateLimit[]): Promise<void> {
  if (limits.length === 0) return
  const fh = limits.find(l => l.kind === 'five_hour')
  const sd = limits.find(l => l.kind === 'seven_day')
  if (!fh && !sd) return

  const session = await $.session.id()
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  if (!home) return
  // Файл свой у каждой сессии: append нет, общий файл терял бы строки.
  const path = `${home.replace(/\\/g, '/')}/.claude/cc-usage/limit-samples/${session}.ndjson`

  const sample: Sample = {
    ts: (await $.clock.now()) / 1000,
    session,
    five_hour_pct: fh ? fh.percentUsed : null,
    five_hour_reset: toUnix(fh?.resetsAt),
    seven_day_pct: sd ? sd.percentUsed : null,
    seven_day_reset: toUnix(sd?.resetsAt),
    fresh: 1,
    source: 'mod',
  }

  // $.fs.write перезаписывает файл целиком: читаем, дописываем строку, пишем.
  const existing = (await $.fs.exists(path)) ? String(await $.fs.read(path)) : ''
  const lines = existing.split('\n').filter(l => l.trim() !== '')
  const lastLine = lines[lines.length - 1]
  if (lastLine !== undefined) {
    let last: Partial<Sample> | undefined
    try {
      last = JSON.parse(lastLine) as Partial<Sample>
    } catch {
      last = undefined // битая строка — просто пишем новую
    }
    if (last && isSame(last, sample) && sample.ts - (last.ts ?? 0) < MIN_INTERVAL_S) return
  }
  const prefix = existing === '' || existing.endsWith('\n') ? existing : existing + '\n'
  await $.fs.write(path, prefix + JSON.stringify(sample) + '\n')
}

const debug = ($: EngineInterface, text: string): void => {
  try {
    $.ui.log(`cc-usage-limits: ${text}`, { to: 'debug' })
  } catch {
    // лог не должен ронять таймер
  }
}

// Один запуск poll-limits. Ничего не бросает: сбой только в debug-лог.
async function poll($: EngineInterface, python: string, cli: string): Promise<void> {
  try {
    const r = await $.process.run([python, cli, 'poll-limits', '--min-interval', POLL_MIN_INTERVAL_S], {
      timeoutMs: POLL_TIMEOUT_MS,
      env: { PYTHONUTF8: '1' },
    })
    debug($, `poll-limits: код ${r.exitCode}: ${(r.stdout + r.stderr).trim()}`)
  } catch (err) {
    debug($, `poll-limits не запустился: ${String(err)}`)
  }
}

const str = (v: unknown): string => (typeof v === 'string' ? v.trim() : '')

export const register: Register = (on, options) => {
  const cli = str(options.cli_path)
  const python = str(options.python) || 'python'
  let stop: (() => void) | undefined

  // Замер после каждого хода основного потока и при сдвиге лимита на 1 п.п.
  on('session.measure', async ($, e, next) => {
    try {
      await record($, e.rateLimits)
    } catch (err) {
      // Любой сбой глотаем: общий worker с другими модами, сессию не ломаем.
      try {
        $.ui.log(`cc-usage-limits: замер не записан: ${String(err)}`, { to: 'debug' })
      } catch {
        // лог тоже не должен ронять хук
      }
    }
    return next(e)
  })

  // Таймер живёт, пока жива сессия: /clear его не трогает (session.start
  // после /clear не приходит, а таймер не сбрасывается — опрос общий на аккаунт).
  // Перезагрузка модуля роняет таймеры, и session.start приходит снова.
  on('session.start', async ($, e, next) => {
    if (cli !== '') {
      try {
        stop?.()
        const first = $.clock.after(POLL_FIRST_MS, () => void poll($, python, cli))
        const tick = $.clock.every(POLL_EVERY_MS, () => void poll($, python, cli))
        stop = () => {
          first.cancel()
          tick.cancel()
        }
      } catch (err) {
        debug($, `таймер не запущен: ${String(err)}`)
      }
    }
    return next(e)
  })
}

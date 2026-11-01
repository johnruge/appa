import { describe, expect, it } from 'vitest'
import { parse, tokenize } from './commands'

describe('tokenize', () => {
  it('splits on whitespace and honors quotes', () => {
    expect(tokenize(`schedule add scout "0 */4 * * *" --board config`)).toEqual([
      'schedule', 'add', 'scout', '0 */4 * * *', '--board', 'config',
    ])
    expect(tokenize(`a 'b c' ""`)).toEqual(['a', 'b c', ''])
  })
  it('rejects an unclosed quote', () => {
    expect(() => tokenize('add "oops')).toThrow('unclosed quote')
  })
})

describe('parse', () => {
  it('poll defaults to your configured boards', () => {
    expect(parse('poll --model qwen/qwen3.6-35b-a3b --max-usd 0.1')).toEqual({
      kind: 'run', role: 'scout', input: { boards: ['config'] }, model: 'qwen/qwen3.6-35b-a3b', budget: { max_usd: 0.1 },
    })
  })
  it('scout with several boards and urls', () => {
    const p = parse('scout --board greenhouse:hooli --board ashby:globex --url https://x.dev/j/1 --max-new 20')
    expect(p).toMatchObject({ kind: 'run', role: 'scout', input: { boards: ['greenhouse:hooli', 'ashby:globex'], urls: ['https://x.dev/j/1'], max_new: 20 } })
  })
  it('add requires real links', () => {
    expect(parse('add https://a.dev/1 https://b.dev/2 --force')).toMatchObject({
      kind: 'run', role: 'scout', input: { urls: ['https://a.dev/1', 'https://b.dev/2'], force: true },
    })
    expect(parse('add javascript:alert(1)')).toMatchObject({ kind: 'error', message: expect.stringContaining('not a link') })
    expect(parse('add')).toMatchObject({ kind: 'error' })
  })
  it('schedules', () => {
    expect(parse('schedule add scout "0 */4 * * *" --board config')).toEqual({
      kind: 'schedule-add', role: 'scout', cron: '0 */4 * * *', input: { boards: ['config'] }, model: undefined, budget: undefined,
    })
    expect(parse('schedule add scout every-day')).toMatchObject({ kind: 'error', message: expect.stringContaining('5 fields') })
    expect(parse('schedule off 3')).toEqual({ kind: 'schedule-set', id: 3, action: 'off' })
    expect(parse('schedules')).toEqual({ kind: 'schedule-list' })
  })
  it('answers', () => {
    expect(parse('answer 7 approve')).toEqual({ kind: 'answer', id: 7, action: 'approve', choice: undefined })
    expect(parse('answer 7 choose https://job-boards.greenhouse.io/x/jobs/1')).toMatchObject({ choice: 'https://job-boards.greenhouse.io/x/jobs/1' })
    expect(parse('answer 7 choose nope')).toMatchObject({ kind: 'error' })
    expect(parse('answer x approve')).toMatchObject({ kind: 'error', message: 'answer needs a numeric id' })
  })
  it('rejects bad options', () => {
    expect(parse('poll --max-usd lots')).toMatchObject({ kind: 'error', message: '--max-usd must be a positive number' })
    expect(parse('poll --rm-rf /')).toMatchObject({ kind: 'error', message: 'unknown option --rm-rf' })
    expect(parse('poll --model')).toMatchObject({ kind: 'error', message: '--model needs a value' })
    expect(parse('frobnicate')).toMatchObject({ kind: 'error', message: expect.stringContaining('unknown command') })
  })
  it('ids and simple commands', () => {
    expect(parse('follow 12')).toEqual({ kind: 'follow', id: 12 })
    expect(parse('cancel -1')).toMatchObject({ kind: 'error' })
    expect(parse('profile edit')).toEqual({ kind: 'profile', edit: true })
    expect(parse('stop-all')).toEqual({ kind: 'stop-all' })
  })
})

describe('feeds and takeover', () => {
  it('parses sweep, listings, listing actions and takeover', () => {
    expect(parse('sweep --max-usd 2')).toEqual({ kind: 'run', role: 'scout', input: { sweep: true }, model: undefined, budget: { max_usd: 2 } })
    expect(parse('listings')).toEqual({ kind: 'listings', status: 'new', q: undefined })
    expect(parse('listings filtered --q stripe')).toEqual({ kind: 'listings', status: 'filtered', q: 'stripe' })
    expect(parse('listings bogus').kind).toBe('error')
    expect(parse('track 3 4')).toEqual({ kind: 'listing-action', action: 'track', ids: [3, 4] })
    expect(parse('restore x').kind).toBe('error')
    expect(parse('takeover 12')).toEqual({ kind: 'takeover', ids: [12] })
    expect(parse('takeover').kind).toBe('error')
    expect(parse('feeds')).toEqual({ kind: 'feeds' })
  })
})

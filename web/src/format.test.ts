import { describe, expect, it } from 'vitest'
import { host, initials, plural, reasons, safeHref, sourceName, splitQuote, timeAgo, timeUntil, toList, usd } from './format'

const NOW = Date.parse('2026-10-31T12:00:00Z')

describe('time', () => {
  it('says how long ago', () => {
    expect(timeAgo('2026-10-31T11:59:40Z', NOW)).toBe('just now')
    expect(timeAgo('2026-10-31T11:55:00Z', NOW)).toBe('5m ago')
    expect(timeAgo('2026-10-31T09:00:00Z', NOW)).toBe('3h ago')
    expect(timeAgo('2026-10-29T12:00:00Z', NOW)).toBe('2d ago')
    expect(timeAgo(null, NOW)).toBe('')
    expect(timeAgo('not a date', NOW)).toBe('')
  })
  it('says how long until', () => {
    expect(timeUntil('2026-10-31T12:20:00Z', NOW)).toBe('in 20m')
    expect(timeUntil('2026-10-31T18:00:00Z', NOW)).toBe('in 6h')
    expect(timeUntil('2026-10-31T11:00:00Z', NOW)).toBe('now')
  })
})

describe('screening reasons', () => {
  it('splits notes into reasons and quotes', () => {
    const notes = 'sponsorship unclear for OPT/future: \'This position is not eligible for visa sponsorship.\'; passes all filters'
    const rs = reasons(notes)
    expect(rs).toHaveLength(2)
    expect(splitQuote(rs[0])).toEqual({ text: 'sponsorship unclear for OPT/future', quote: 'This position is not eligible for visa sponsorship.' })
    expect(splitQuote('passes all filters')).toEqual({ text: 'passes all filters', quote: null })
    expect(reasons(null)).toEqual([])
  })
})

describe('labels', () => {
  it('formats names, hosts and money', () => {
    expect(initials('Boston Scientific')).toBe('BS')
    expect(initials('stripe')).toBe('ST')
    expect(initials('')).toBe('?')
    expect(host('https://www.example.com/jobs/1')).toBe('example.com')
    expect(host('nope')).toBe('')
    expect(sourceName('pipeline:feed:simplify_newgrad')).toBe('Simplify new grad')
    expect(sourceName('board:workday')).toBe('Workday')
    expect(usd(0)).toBe('$0')
    expect(usd(0.0061)).toBe('$0.0061')
    expect(usd(1.5)).toBe('$1.50')
    expect(plural(1, 'match', 'matches')).toBe('1 match')
    expect(plural(3, 'match', 'matches')).toBe('3 matches')
    expect(toList(' senior, staff ,, intern ')).toEqual(['senior', 'staff', 'intern'])
  })
})

describe('links', () => {
  it('only renders http(s) links', () => {
    expect(safeHref('https://jobs.example.com/1')).toBe('https://jobs.example.com/1')
    expect(safeHref('javascript:alert(1)')).toBeNull()
    expect(safeHref('data:text/html,hi')).toBeNull()
    expect(safeHref('not a url')).toBeNull()
    expect(safeHref(null)).toBeNull()
  })
})

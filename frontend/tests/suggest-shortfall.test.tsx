import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SuggestPage from '@/app/dashboard/suggest/page'
import type { Outfit, SuggestionBatchInfo, SuggestionBatchResponse } from '@/lib/types'
import { api, ApiError } from '@/lib/api'

vi.mock('@/lib/api', () => {
  class ApiError extends Error {
    constructor(message: string, public status: number, public data: unknown) { super(message) }
  }
  return { api: { post: vi.fn(), get: vi.fn() }, ApiError }
})
// Show interpolated values so "2 of 3" is visible to the assertions.
vi.mock('next-intl', () => ({
  useLocale: () => 'en',
  useFormatter: () => ({ number: String, list: (items: string[]) => items.join(', ') }),
  useTranslations: () => {
    const t = (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${Object.entries(values).map(([k, v]) => `${k}=${v}`).join(' ')}` : key
    t.raw = (key: string) => key
    t.rich = (key: string) => key
    t.markup = (key: string) => key
    t.has = () => true
    return t
  },
}))
vi.mock('@/lib/hooks/use-user', () => ({ useUserToday: () => () => '2026-10-30' }))
vi.mock('@/lib/hooks/use-weather', () => ({ useWeather: () => ({ data: { temperature: 20, feels_like: 20, condition: 'clear', wind_speed: 0, precipitation_chance: 0 }, isFetching: false, isError: false }) }))
vi.mock('@/lib/hooks/use-items', () => ({ useItem: () => ({ data: undefined }) }))
vi.mock('@/lib/hooks/use-preferences', () => ({ usePreferences: () => ({ data: { default_occasion: 'casual' } }) }))
vi.mock('@/components/shared/item-picker', () => ({ ItemPicker: () => null }))
vi.mock('@/lib/hooks/use-translated-constants', () => ({
  useOccasionLabel: () => (v: string) => v, useTypeLabel: () => (v: string) => v,
  useWeatherConditionLabel: () => (v: string) => v,
  useOccasionOptions: () => [{ value: 'casual', label: 'casual' }],
}))

const batchInfo = (saved: number, extra: Partial<SuggestionBatchInfo> = {}): SuggestionBatchInfo => ({
  id: 'batch-1', target_count: 3, saved_count: saved, missing_count: 3 - saved,
  can_fill: saved < 3, filling: false, expired: false, fill_attempts_remaining: 3, ...extra,
})
const look = (id: string, saved = 2) => ({
  id, scheduled_for: '2026-10-31', status: 'pending', occasion: 'casual', source: 'on_demand',
  items: [], feedback: null, weather: null, reasoning: `look ${id}`, highlights: [],
  suggestion_batch: batchInfo(saved),
} as unknown as Outfit)
const filled = (): SuggestionBatchResponse => ({
  batch: batchInfo(3),
  // The server copy of "one" has no reaction yet; the page must keep its own.
  outfits: [look('one'), look('two'), look('three')],
  added_outfit_ids: ['three'],
})
const FILL_URL = '/outfits/suggestion-batches/batch-1/fill'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

const renderPage = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <SuggestPage />
  </QueryClientProvider>
)

async function generate(options: Outfit[]) {
  vi.mocked(api.post).mockImplementation(async (url: string, body?: unknown) => {
    if (url === '/outfits/suggest-options') return options
    if (url.endsWith('/reaction')) {
      return { ...look(url.split('/')[2]), status: 'accepted', feedback: { worn_at: null } }
    }
    throw new Error(`unexpected ${url} ${JSON.stringify(body)}`)
  })
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: 'getSuggestion' }))
  await screen.findAllByRole('button', { name: /options.optionNumber/ })
}

const fillButton = () => screen.getByRole('button', { name: /shortfall\.(fill|filling)/ })

beforeEach(() => {
  vi.mocked(api.post).mockReset()
  vi.mocked(api.get).mockReset()
})
afterEach(() => vi.useRealTimers())

describe('suggestion shortfall', () => {
  it('shows nothing extra when all three outfits arrive', async () => {
    await generate([look('one', 3), look('two', 3), look('three', 3)])
    expect(screen.queryByTestId('outfit-shortfall')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /options.optionNumber/ })).toHaveLength(3)
  })

  it('says 2 of 3 and offers to complete only the missing outfit', async () => {
    await generate([look('one'), look('two')])
    expect(screen.getByTestId('outfit-shortfall')).toHaveTextContent('shortfall.title saved=2 target=3')
    expect(fillButton()).toHaveTextContent('shortfall.fill count=1')
    expect(fillButton()).toBeEnabled()
  })

  it('adds the new outfit, keeps the existing reactions and ignores a double click', async () => {
    await generate([look('one'), look('two')])
    const like = screen.getByRole('button', { name: /reaction.like reaction.likeHint/ })
    fireEvent.click(like)
    await waitFor(() => expect(like).toHaveAttribute('aria-pressed', 'true'))

    const pending = deferred<SuggestionBatchResponse>()
    vi.mocked(api.post).mockImplementation(async (url: string) => {
      if (url === FILL_URL) return pending.promise
      throw new Error(url)
    })
    fireEvent.click(fillButton())
    fireEvent.click(fillButton())
    expect(fillButton()).toBeDisabled()
    expect(fillButton()).toHaveTextContent('shortfall.filling')
    expect(fillButton()).toHaveAttribute('aria-busy', 'true')
    await act(async () => pending.resolve(filled()))

    expect(vi.mocked(api.post).mock.calls.filter(([url]) => url === FILL_URL)).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /options.optionNumber/ })).toHaveLength(3)
    expect(screen.queryByTestId('outfit-shortfall')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('shortfall.added count=1')
    // The page moved to the new option; the first one still carries its reaction.
    expect(screen.getByText('look three')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'options.optionNumber number=1' }))
    expect(screen.getByRole('button', { name: /reaction.like reaction.likeHint/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps the current outfits and explains a failed fill', async () => {
    await generate([look('one'), look('two')])
    vi.mocked(api.post).mockRejectedValueOnce(
      new ApiError('AI down', 503, { detail: { code: 'ai_unavailable', message: 'AI down' } }),
    )
    fireEvent.click(fillButton())
    expect(await screen.findByText('shortfall.errors.aiUnavailable')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /options.optionNumber/ })).toHaveLength(2)
    expect(screen.getByText('look one')).toBeInTheDocument()
    expect(fillButton()).toBeEnabled()
  })

  it('stops offering a fill once the attempt limit is reached', async () => {
    await generate([look('one'), look('two')])
    vi.mocked(api.post).mockRejectedValueOnce(
      new ApiError('limit', 429, { detail: { code: 'fill_limit_reached', message: 'limit' } }),
    )
    fireEvent.click(fillButton())
    expect(await screen.findByText('shortfall.errors.limitReached')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /shortfall\.fill/ })).not.toBeInTheDocument()
  })

  it('waits for a fill already running instead of starting another', async () => {
    await generate([look('one'), look('two')])
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(api.post).mockRejectedValueOnce(
      new ApiError('busy', 409, { detail: { code: 'fill_in_progress', message: 'busy' } }),
    )
    vi.mocked(api.get)
      .mockResolvedValueOnce({ ...filled(), batch: batchInfo(2, { filling: true, can_fill: false }), outfits: [look('one'), look('two')], added_outfit_ids: [] })
      .mockResolvedValueOnce(filled())
    fireEvent.click(fillButton())
    await act(async () => { await vi.advanceTimersByTimeAsync(6500) })

    await waitFor(() => expect(screen.getAllByRole('button', { name: /options.optionNumber/ })).toHaveLength(3))
    expect(api.get).toHaveBeenCalledWith('/outfits/suggestion-batches/batch-1')
    expect(vi.mocked(api.post).mock.calls.filter(([url]) => url === FILL_URL)).toHaveLength(1)
  })
})

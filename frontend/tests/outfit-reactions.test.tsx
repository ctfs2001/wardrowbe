import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { OutfitReactionActions } from '@/components/outfit-reaction-actions'
import SuggestPage from '@/app/dashboard/suggest/page'
import type { Outfit } from '@/lib/types'
import { api } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: { post: vi.fn() }, ApiError: class extends Error {} }))
vi.mock('@/lib/hooks/use-user', () => ({ useUserToday: () => () => '2026-10-26' }))
vi.mock('@/lib/hooks/use-weather', () => ({ useWeather: () => ({ data: { temperature: 20, feels_like: 20, condition: 'clear', wind_speed: 0, precipitation_chance: 0 }, isFetching: false, isError: false }) }))
vi.mock('@/lib/hooks/use-items', () => ({ useItem: () => ({ data: undefined }) }))
vi.mock('@/lib/hooks/use-preferences', () => ({ usePreferences: () => ({ data: { default_occasion: 'casual' } }) }))
vi.mock('@/components/shared/item-picker', () => ({ ItemPicker: () => null }))
vi.mock('@/lib/hooks/use-translated-constants', () => ({
  useOccasionLabel: () => (v: string) => v, useTypeLabel: () => (v: string) => v,
  useWeatherConditionLabel: () => (v: string) => v,
  useOccasionOptions: () => [{ value: 'casual', label: 'casual' }],
}))

const look = (id: string, date = '2026-10-26') => ({
  id, scheduled_for: date, status: 'pending', occasion: 'casual', source: 'on_demand',
  items: [], feedback: null, weather: null, reasoning: id, highlights: [],
} as unknown as Outfit)
const renderWithQueries = (ui: React.ReactNode) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>{ui}</QueryClientProvider>
)

beforeEach(() => {
  vi.mocked(api.post).mockReset().mockImplementation(async (url, body) => {
    if (url === '/outfits/suggest-options') return [look('one'), look('two'), look('three')]
    const action = (body as { action: string } | undefined)?.action
    return { ...look(url.split('/')[2]), status: action === 'dislike' ? 'rejected' : 'accepted',
      feedback: { worn_at: action === 'wear' ? '2026-10-26' : null } }
  })
})

describe('unified outfit reactions', () => {
  it.each([['accept', 'wear'], ['like', 'like'], ['dislike', 'dislike']])('sends only the explicit %s choice', async (label, action) => {
    renderWithQueries(<OutfitReactionActions outfit={look('one')} />)
    expect(api.post).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`reaction.${label} reaction.${label}Hint`) }))
    await waitFor(() => expect(api.post).toHaveBeenCalledExactlyOnceWith('/outfits/one/reaction', { action }))
  })

  it('allows liking tomorrow but disables recording a future wear', () => {
    renderWithQueries(<OutfitReactionActions outfit={look('one', '2026-10-27')} />)
    expect(screen.getByRole('button', { name: /reaction.accept reaction.acceptHint/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /reaction.like reaction.likeHint/ })).toBeEnabled()
  })

  it('keeps a failed choice unselected and shows an error', async () => {
    vi.mocked(api.post).mockRejectedValueOnce(new Error('offline'))
    renderWithQueries(<OutfitReactionActions outfit={look('one')} />)
    fireEvent.click(screen.getByRole('button', { name: /reaction.like reaction.likeHint/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('reaction.error')
    expect(screen.getByRole('button', { name: /reaction.like reaction.likeHint/ })).toHaveAttribute('aria-pressed', 'false')
  })

  it('keeps all three options after liking and only skips untouched alternatives when starting over', async () => {
    renderWithQueries(<SuggestPage />)
    fireEvent.click(screen.getByRole('button', { name: 'getSuggestion' }))
    const likeButton = await screen.findByRole('button', { name: /reaction.like reaction.likeHint/ })
    fireEvent.click(likeButton)
    await waitFor(() => expect(likeButton).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.getAllByRole('button', { name: 'options.optionNumber' })).toHaveLength(3)
    expect(api.post).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole('button', { name: 'startOver' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/outfits/two/skip'))
    expect(api.post).toHaveBeenCalledWith('/outfits/three/skip')
    expect(api.post).not.toHaveBeenCalledWith('/outfits/one/skip')
  })
})

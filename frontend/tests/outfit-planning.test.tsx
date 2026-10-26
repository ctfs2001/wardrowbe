import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { OutfitPlanning } from '@/components/outfit-planning'
import type { ActivityNeed, WearDay, WearPeriod } from '@/lib/types'
import SuggestPage from '@/app/dashboard/suggest/page'
import { api } from '@/lib/api'
import { useWeather } from '@/lib/hooks/use-weather'

vi.mock('@/lib/api', () => ({ api: { post: vi.fn() }, ApiError: class extends Error {} }))
vi.mock('@/lib/hooks/use-weather', () => ({ useWeather: vi.fn() }))
vi.mock('@/lib/hooks/use-items', () => ({ useItem: () => ({ data: undefined }) }))
vi.mock('@/lib/hooks/use-preferences', () => ({ usePreferences: () => ({ data: { default_occasion: 'casual' } }) }))
vi.mock('@/components/shared/item-picker', () => ({ ItemPicker: () => null }))
vi.mock('@/lib/hooks/use-translated-constants', () => ({
  useOccasionLabel: () => (v: string) => v,
  useOccasionOptions: () => [{ value: 'casual', label: 'casual' }],
  useWeatherConditionLabel: () => (v: string) => v,
  useTypeLabel: () => (v: string) => v,
}))

function Harness() {
  const [day, setDay] = useState<WearDay>('today')
  const [period, setPeriod] = useState<WearPeriod>('full day')
  const [needs, setNeeds] = useState<ActivityNeed[]>([])
  return <OutfitPlanning day={day} period={period} needs={needs} onDayChange={setDay} onPeriodChange={setPeriod} onNeedsChange={setNeeds} />
}

beforeEach(() => {
  vi.mocked(api.post).mockReset().mockResolvedValue([])
  vi.mocked(useWeather).mockReturnValue({
    data: { temperature: 20, feels_like: 19, humidity: 60, precipitation_chance: 20,
      precipitation_mm: 0, wind_speed: 5, condition: 'clear', condition_code: 0,
      is_day: true, uv_index: 3, timestamp: '', target_date: '2026-10-27', timezone: 'Asia/Taipei' },
    isFetching: false, isError: false, refetch: vi.fn(),
  } as unknown as ReturnType<typeof useWeather>)
})

describe('outfit planning', () => {
  it('selects day and period independently and toggles multiple needs', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'planning.tomorrow' }))
    fireEvent.click(screen.getByRole('button', { name: 'planning.morning' }))
    fireEvent.click(screen.getByRole('button', { name: 'planning.walking' }))
    fireEvent.click(screen.getByRole('button', { name: 'planning.indoorAc' }))
    expect(screen.getByRole('button', { name: 'planning.tomorrow' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'planning.morning' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'planning.walking' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'planning.walking' }))
    expect(screen.getByRole('button', { name: 'planning.walking' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'planning.indoorAc' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('uses the chosen day and time for both weather preview and the generation request', async () => {
    render(<SuggestPage />)
    fireEvent.click(screen.getByRole('button', { name: 'planning.tomorrow' }))
    fireEvent.click(screen.getByRole('button', { name: 'planning.morning' }))
    fireEvent.click(screen.getByRole('button', { name: 'planning.walking' }))
    fireEvent.change(screen.getByLabelText('eventNotes.title'), { target: { value: 'Meet friends' } })
    fireEvent.click(screen.getByRole('button', { name: 'getSuggestion' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/outfits/suggest-options', {
      occasion: 'casual', include_items: [], wear_day: 'tomorrow', time_of_day: 'morning',
      activity_needs: ['walking'], event_notes: 'Meet friends',
    }))
    expect(useWeather).toHaveBeenCalledWith('tomorrow', 'morning')
  })

  it('shows a forecast error and prevents generating with an unavailable preview', () => {
    vi.mocked(useWeather).mockReturnValue({ data: undefined, isFetching: false, isError: true, refetch: vi.fn() } as unknown as ReturnType<typeof useWeather>)
    render(<SuggestPage />)
    expect(screen.getByText('planning.forecastError')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'getSuggestion' })).toBeDisabled()
    expect(screen.queryByText('location.notSet')).not.toBeInTheDocument()
  })
})

'use client';

import { useQuery } from '@tanstack/react-query';
import { useSession } from 'next-auth/react';
import { api } from '@/lib/api';
import { useSetTokenIfAvailable } from '@/lib/hooks/use-session-token';
import type { CurrentWeather, WearDay, WearPeriod } from '@/lib/types';
import { queryKeys } from '@/lib/hooks/query-keys';
import { WEATHER_STALE_TIME } from '@/lib/hooks/query-timing';

export function useWeather(wearDay?: WearDay, period: WearPeriod = 'full day') {
  const { status } = useSession();
  useSetTokenIfAvailable();

  return useQuery({
    queryKey: wearDay ? [...queryKeys.weather, wearDay, period] : queryKeys.weather,
    queryFn: () => api.get<CurrentWeather>(wearDay
      ? `/weather/current?wear_day=${wearDay}&time_of_day=${encodeURIComponent(period)}`
      : '/weather/current'),
    enabled: status !== 'loading',
    staleTime: wearDay ? 0 : WEATHER_STALE_TIME,
    refetchInterval: wearDay ? 60_000 : false,
    retry: false, // Don't retry if location not set
  });
}

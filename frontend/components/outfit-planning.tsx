'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { ActivityNeed, WearDay, WearPeriod } from '@/lib/types';

interface Props {
  day: WearDay;
  period: WearPeriod;
  needs: ActivityNeed[];
  onDayChange: (day: WearDay) => void;
  onPeriodChange: (period: WearPeriod) => void;
  onNeedsChange: (needs: ActivityNeed[]) => void;
  disabled?: boolean;
}

export function OutfitPlanning({ day, period, needs, onDayChange, onPeriodChange, onNeedsChange, disabled }: Props) {
  const t = useTranslations('suggest');
  const days = [
    { value: 'today' as const, label: t('planning.today') },
    { value: 'tomorrow' as const, label: t('planning.tomorrow') },
  ];
  const periods = [
    { value: 'morning' as const, label: t('planning.morning') },
    { value: 'afternoon' as const, label: t('planning.afternoon') },
    { value: 'evening' as const, label: t('planning.evening') },
    { value: 'full day' as const, label: t('planning.fullDay') },
  ];
  const options = [
    { value: 'indoor_ac' as const, label: t('planning.indoorAc') },
    { value: 'outdoors' as const, label: t('planning.outdoors') },
    { value: 'walking' as const, label: t('planning.walking') },
    { value: 'rain_ready' as const, label: t('planning.rainReady') },
    { value: 'easy_move' as const, label: t('planning.easyMove') },
    { value: 'polished' as const, label: t('planning.polished') },
  ];
  return (
    <div className="space-y-4">
      <fieldset disabled={disabled} className="space-y-2">
        <legend className="font-semibold mb-2">{t('planning.day')}</legend>
        <div className="flex flex-wrap gap-2">
          {days.map(option => <Button key={option.value} type="button" variant={day === option.value ? 'default' : 'outline'} aria-pressed={day === option.value} onClick={() => onDayChange(option.value)}>{option.label}</Button>)}
        </div>
      </fieldset>
      <fieldset disabled={disabled} className="space-y-2">
        <legend className="font-semibold mb-2">{t('planning.period')}</legend>
        <div className="flex flex-wrap gap-2">
          {periods.map(option => <Button key={option.value} type="button" variant={period === option.value ? 'default' : 'outline'} aria-pressed={period === option.value} onClick={() => onPeriodChange(option.value)}>{option.label}</Button>)}
        </div>
        <p className="text-xs text-muted-foreground">{t('planning.hours')}</p>
      </fieldset>
      <fieldset disabled={disabled} className="space-y-2">
        <legend className="font-semibold mb-2">{t('planning.needs')}</legend>
        <p className="text-xs text-muted-foreground">{t('planning.needsHint')}</p>
        <div className="flex flex-wrap gap-2">
          {options.map(option => <Button key={option.value} type="button" size="sm" variant={needs.includes(option.value) ? 'default' : 'outline'} aria-pressed={needs.includes(option.value)} onClick={() => onNeedsChange(needs.includes(option.value) ? needs.filter(n => n !== option.value) : [...needs, option.value])}>{option.label}</Button>)}
        </div>
      </fieldset>
    </div>
  );
}

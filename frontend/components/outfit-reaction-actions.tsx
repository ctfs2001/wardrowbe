'use client';

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useOutfitReaction, type OutfitReaction, type Outfit } from '@/lib/hooks/use-outfits';
import { useUserToday } from '@/lib/hooks/use-user';

export function OutfitReactionActions({ outfit, onUpdate }: {
  outfit: Outfit;
  onUpdate?: (outfit: Outfit) => void;
}) {
  const t = useTranslations('common');
  const reaction = useOutfitReaction();
  const getToday = useUserToday();
  const [current, setCurrent] = useState(outfit);
  const [failed, setFailed] = useState(false);
  useEffect(() => setCurrent(outfit), [outfit]);
  const worn = !!current.feedback?.worn_at;
  const future = !current.scheduled_for || current.scheduled_for > getToday();
  const selected = current.status === 'rejected' ? 'dislike'
    : current.status === 'accepted' ? (worn ? 'wear' : 'like') : null;
  const choices = [
    { action: 'wear' as const, label: t('reaction.accept'), hint: t('reaction.acceptHint') },
    { action: 'like' as const, label: t('reaction.like'), hint: t('reaction.likeHint') },
    { action: 'dislike' as const, label: t('reaction.dislike'), hint: t('reaction.dislikeHint') },
  ];
  async function respond(action: OutfitReaction) {
    setFailed(false);
    try {
      const updated = await reaction.mutateAsync({ outfitId: outfit.id, action });
      setCurrent(updated);
      onUpdate?.(updated);
    } catch {
      setFailed(true);
    }
  }
  return (
    <div className="space-y-2 w-full">
      <div className="grid grid-cols-3 gap-2">
        {choices.map(({ action, label, hint }) => (
          <Button key={action} type="button" variant={selected === action ? 'default' : 'outline'}
            className="h-auto py-2 px-1 flex flex-col gap-0.5 whitespace-normal"
            aria-pressed={selected === action} disabled={reaction.isPending || (action === 'wear' && future)}
            onClick={() => respond(action)}>
            <span>{label}</span>{' '}<span className="text-[10px] font-normal">{hint}</span>
          </Button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{t('reaction.neutralHint')}</p>
      {future && <p className="text-xs text-muted-foreground">{t('reaction.futureHint')}</p>}
      {worn && <p className="text-xs text-muted-foreground">{t('reaction.wornOn', { date: current.feedback!.worn_at! })}</p>}
      {failed && <p role="alert" className="text-xs text-destructive">{t('reaction.error')}</p>}
    </div>
  );
}

"use client";

import { useMemo } from "react";

import type { RideCard as RideCardData } from "@/lib/types";

import { ProviderGroup } from "./ProviderGroup";

/** Provider display order; unknown providers fall to the end alphabetically. */
const PROVIDER_ORDER: Record<string, number> = {
  uber: 0,
  lyft: 1,
  waymo: 2,
};

/**
 * Ride options grouped by provider with all ride types under each
 * (Requirement 2.3). Input cards are already price-sorted within provider by
 * the session-stream hook; here we just bucket them by provider in display
 * order. Renders nothing until at least one quote has arrived.
 */
export function RideCards({ cards }: { cards: RideCardData[] }) {
  const groups = useMemo(() => {
    const byProvider = new Map<string, RideCardData[]>();
    for (const card of cards) {
      const list = byProvider.get(card.provider);
      if (list) {
        list.push(card);
      } else {
        byProvider.set(card.provider, [card]);
      }
    }
    return [...byProvider.entries()].sort((a, b) => {
      const oa = PROVIDER_ORDER[a[0]] ?? 99;
      const ob = PROVIDER_ORDER[b[0]] ?? 99;
      return oa - ob || a[0].localeCompare(b[0]);
    });
  }, [cards]);

  if (cards.length === 0) {
    return null;
  }

  return (
    <div className="ride-cards" aria-label="Ride options">
      {groups.map(([provider, providerCards]) => (
        <ProviderGroup key={provider} provider={provider} cards={providerCards} />
      ))}
    </div>
  );
}

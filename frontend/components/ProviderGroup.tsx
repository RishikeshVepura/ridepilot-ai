import { providerLabel } from "@/lib/format";
import type { RideCard as RideCardData } from "@/lib/types";

import { RideCard } from "./RideCard";

/** A provider header plus all of its ride-type cards (Requirement 2.3). */
export function ProviderGroup({
  provider,
  cards,
}: {
  provider: string;
  cards: RideCardData[];
}) {
  return (
    <section className="provider-group" aria-label={`${providerLabel(provider)} rides`}>
      <h3 className="provider-group__title">
        <span className={`provider-dot provider-dot--${provider}`} aria-hidden="true" />
        {providerLabel(provider)}
      </h3>
      <div className="provider-group__cards">
        {cards.map((card) => (
          <RideCard key={card.ride_type} card={card} />
        ))}
      </div>
    </section>
  );
}

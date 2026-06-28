"use client";

import { useEffect, useRef, useState } from "react";

import { formatMinutes, formatPrice } from "@/lib/format";
import type { RideCard as RideCardData } from "@/lib/types";

/**
 * A single ride option card: ride type, price, pickup ETA, trip duration, and
 * availability. When its `updatedAt` changes (a `quote_update` touched it) the
 * card briefly flashes a highlight so silent price/ETA refreshes are visible
 * without being noisy (Requirement 3.3).
 */
export function RideCard({ card }: { card: RideCardData }) {
  const [highlight, setHighlight] = useState(false);
  // Skip the very first render so cards don't all flash when first seeded.
  const lastUpdate = useRef<number | null>(null);

  useEffect(() => {
    if (lastUpdate.current === null) {
      lastUpdate.current = card.updatedAt;
      return;
    }
    if (card.updatedAt === lastUpdate.current) {
      return;
    }
    lastUpdate.current = card.updatedAt;
    setHighlight(true);
    const timer = setTimeout(() => setHighlight(false), 1200);
    return () => clearTimeout(timer);
  }, [card.updatedAt]);

  const unavailable = !card.available || card.price == null;

  return (
    <div
      className={[
        "ride-card",
        highlight ? "ride-card--updated" : "",
        unavailable ? "ride-card--unavailable" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="ride-card__top">
        <span className="ride-card__type">{card.ride_type}</span>
        <span className="ride-card__price">
          {formatPrice(card.price, card.currency)}
        </span>
      </div>
      <div className="ride-card__meta">
        <span className="ride-card__meta-item">
          <span className="ride-card__meta-label">Pickup</span>
          {formatMinutes(card.pickup_eta_minutes)}
        </span>
        <span className="ride-card__meta-item">
          <span className="ride-card__meta-label">Trip</span>
          {formatMinutes(card.trip_duration_minutes)}
        </span>
      </div>
      {unavailable ? (
        <span className="ride-card__badge">Unavailable</span>
      ) : null}
    </div>
  );
}

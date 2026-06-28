"use client";

import { formatStatus } from "@/lib/format";
import type { BookingState, RideStatusEntry } from "@/lib/types";

/**
 * Booking + ride lifecycle area. Shows the current booking status (from
 * `booking_update`) as a pill and the ride milestone timeline (from
 * `ride_status` events) such as "Your driver is arriving." (Requirement 6.3).
 * Renders nothing until there is a booking or at least one milestone.
 */
export function RideStatusPanel({
  booking,
  rideStatuses,
}: {
  booking: BookingState | null;
  rideStatuses: RideStatusEntry[];
}) {
  if (!booking && rideStatuses.length === 0) {
    return null;
  }

  return (
    <section className="ride-status" aria-label="Booking status">
      {booking ? (
        <div className="ride-status__header">
          <span className="ride-status__label">Booking</span>
          <span
            className={`ride-status__pill ride-status__pill--${booking.status.toLowerCase()}`}
          >
            {formatStatus(booking.status)}
          </span>
        </div>
      ) : null}

      {rideStatuses.length > 0 ? (
        <ol className="ride-status__timeline">
          {rideStatuses.map((entry) => (
            <li key={entry.id} className="ride-status__milestone">
              <span className="ride-status__dot" aria-hidden="true" />
              <span className="ride-status__text">{entry.message}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

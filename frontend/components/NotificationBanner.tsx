"use client";

import type { NotificationEntry } from "@/lib/types";

/**
 * Prominent banners for AI notifications — meaningful spoken messages such as
 * "There's a new cheapest option" (Requirement 3.4). Each is dismissible. Task
 * 7.3 will add TTS for these; here we only display/route them, no speech.
 */
export function NotificationBanner({
  notifications,
  onDismiss,
}: {
  notifications: NotificationEntry[];
  onDismiss: (id: string) => void;
}) {
  if (notifications.length === 0) {
    return null;
  }

  return (
    <div className="notifications" role="status" aria-live="polite">
      {notifications.map((n) => (
        <div key={n.id} className="notification">
          <span className="notification__icon" aria-hidden="true">
            🔔
          </span>
          <span className="notification__message">{n.message}</span>
          <button
            type="button"
            className="notification__dismiss"
            aria-label="Dismiss notification"
            onClick={() => onDismiss(n.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

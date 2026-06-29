"use client";

import { useEffect, useRef } from "react";

import type { NotificationEntry } from "@/lib/types";

/** How long a notification banner stays before auto-dismissing (ms). */
const AUTO_DISMISS_MS = 15_000;

/**
 * Prominent banners for AI notifications — meaningful spoken messages such as
 * "Waymo Robotaxi now has the fastest pickup at 6 minutes" (Requirement 3.4).
 *
 * Each banner can be dismissed manually, and also auto-dismisses after
 * AUTO_DISMISS_MS with a countdown progress bar showing the time remaining. The
 * same message is also added to the chat transcript (see ChatWindow), so
 * dismissing a banner never loses the information.
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
        <NotificationItem key={n.id} notification={n} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function NotificationItem({
  notification,
  onDismiss,
}: {
  notification: NotificationEntry;
  onDismiss: (id: string) => void;
}) {
  // Hold the latest onDismiss in a ref so the auto-dismiss timer is armed exactly
  // once per notification (keyed on its id) and isn't reset when the parent
  // re-renders with a new onDismiss identity — which would restart the countdown.
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;

  useEffect(() => {
    const timer = setTimeout(
      () => onDismissRef.current(notification.id),
      AUTO_DISMISS_MS,
    );
    return () => clearTimeout(timer);
  }, [notification.id]);

  return (
    <div className="notification">
      <span className="notification__icon" aria-hidden="true">
        🔔
      </span>
      <span className="notification__message">{notification.message}</span>
      <button
        type="button"
        className="notification__dismiss"
        aria-label="Dismiss notification"
        onClick={() => onDismiss(notification.id)}
      >
        ×
      </button>
      {/* Countdown bar; duration is driven from the same constant as the timer. */}
      <span
        className="notification__progress"
        style={{ animationDuration: `${AUTO_DISMISS_MS}ms` }}
        aria-hidden="true"
      />
    </div>
  );
}

"use client";

/**
 * useSessionStream — owns the server-push side of a chat session (design
 * section 9): the long-lived GET SSE stream at
 * `GET /api/stream/{user_id}/{chat_session_id}`.
 *
 * Why a separate hook (and not useChat's `onEvent`)?
 *   Background events (`quote_update`, `ai_notification`, `booking_update`,
 *   `ride_status`) can arrive on BOTH the POST `/api/chat/message` response
 *   (surfaced via useChat's `onEvent`) AND this dedicated GET stream. To avoid
 *   double-applying deltas, we pick ONE path for non-token state: this GET
 *   stream is the single source of truth for ride cards, notifications, and
 *   booking/ride status. useChat remains responsible only for the transcript
 *   (user/assistant `token` text). The two never write the same state.
 *
 * Why `EventSource` (not the fetch-based `parseSSEStream`)?
 *   This endpoint is a plain long-lived GET, which is exactly what the browser
 *   `EventSource` API is for — and it gives us automatic reconnection with
 *   backoff for free. The fetch reader in `lib/sse.ts` exists for the POST
 *   response, whose body cannot be consumed by `EventSource`.
 *
 * Ride-card state (Requirements 2.3, 3.3):
 *   Cards are held in a Map keyed by `${provider}::${ride_type}`. A
 *   `quote_snapshot` seeds the full set; each `quote_update` merges only the
 *   changed options into existing cards (or adds newly-appeared ones), bumping
 *   `updatedAt` so the UI can flash a subtle highlight. A null `new_price`
 *   marks the option unavailable.
 */

import { useEffect, useRef, useState } from "react";

import { sessionStopUrl, sessionStreamUrl } from "@/lib/config";
import type {
  BookingState,
  NotificationEntry,
  Quote,
  QuoteDeltaChange,
  RideCard,
  RideStatusEntry,
  ServerEvent,
} from "@/lib/types";

/** Stable map key for a ride option. */
function cardKey(provider: string, rideType: string): string {
  return `${provider}::${rideType}`;
}

let entryCounter = 0;
/** Process-unique id for notification / status list entries. */
function nextEntryId(prefix: string): string {
  entryCounter += 1;
  return `${prefix}_${Date.now()}_${entryCounter}`;
}

export interface UseSessionStreamResult {
  /** Live ride cards, newest snapshot/delta applied. */
  rideCards: RideCard[];
  /** AI notification banners, newest last. */
  notifications: NotificationEntry[];
  /** Ride milestone timeline, newest last. */
  rideStatuses: RideStatusEntry[];
  /** Current booking state, or null before any booking_update. */
  booking: BookingState | null;
  /** True while the EventSource connection is open. */
  connected: boolean;
  /** Dismiss a single notification banner. */
  dismissNotification: (id: string) => void;
}

/**
 * Open (and keep open) the per-session server-push stream.
 *
 * @param userId The current user id (from useChat).
 * @param chatSessionId The active chat session id, or null before the first
 *   reply. The stream only opens once this is known, and is torn down and
 *   reopened if it changes.
 */
export function useSessionStream(
  userId: string,
  chatSessionId: string | null,
): UseSessionStreamResult {
  // Cards live in a ref-backed Map so merges read the freshest value without
  // depending on a render; a parallel state list drives rendering.
  const cardsRef = useRef<Map<string, RideCard>>(new Map());
  const [rideCards, setRideCards] = useState<RideCard[]>([]);
  const [notifications, setNotifications] = useState<NotificationEntry[]>([]);
  const [rideStatuses, setRideStatuses] = useState<RideStatusEntry[]>([]);
  const [booking, setBooking] = useState<BookingState | null>(null);
  const [connected, setConnected] = useState(false);

  /** Publish the current card map to render state (sorted for stable display). */
  const flushCards = () => {
    setRideCards(sortCards([...cardsRef.current.values()]));
  };

  /** Replace the whole card set from a snapshot. */
  const seedFromSnapshot = (quotes: Quote[]) => {
    const now = Date.now();
    const next = new Map<string, RideCard>();
    for (const q of quotes) {
      next.set(cardKey(q.provider, q.ride_type), {
        provider: q.provider,
        ride_type: q.ride_type,
        price: q.price,
        currency: q.currency ?? "USD",
        pickup_eta_minutes: q.pickup_eta_minutes ?? null,
        trip_duration_minutes: q.trip_duration_minutes ?? null,
        available: q.available ?? true,
        updatedAt: now,
      });
    }
    cardsRef.current = next;
    flushCards();
  };

  /** Merge only the changed options from a delta into the card map. */
  const applyDelta = (changes: QuoteDeltaChange[]) => {
    const now = Date.now();
    const map = cardsRef.current;
    for (const change of changes) {
      const key = cardKey(change.provider, change.ride_type);
      const existing = map.get(key);
      // new_price === null => the option disappeared / is unavailable.
      const disappeared = change.new_price === null;

      if (existing) {
        map.set(key, {
          ...existing,
          price: change.new_price ?? existing.price,
          pickup_eta_minutes:
            change.new_pickup_eta_minutes ?? existing.pickup_eta_minutes,
          available: !disappeared,
          updatedAt: now,
        });
      } else if (!disappeared) {
        // Newly appeared option (old_price was null). Seed a fresh card; we may
        // not know currency/trip duration from a delta, so use sensible defaults.
        map.set(key, {
          provider: change.provider,
          ride_type: change.ride_type,
          price: change.new_price,
          currency: "USD",
          pickup_eta_minutes: change.new_pickup_eta_minutes ?? null,
          trip_duration_minutes: null,
          available: true,
          updatedAt: now,
        });
      }
    }
    flushCards();
  };

  useEffect(() => {
    // Nothing to stream until the backend has minted a chat session id.
    if (!chatSessionId) {
      return;
    }

    const url = sessionStreamUrl(userId, chatSessionId);
    const source = new EventSource(url);

    source.onopen = () => setConnected(true);
    source.onerror = () => {
      // EventSource auto-reconnects; reflect the transient drop in the UI.
      setConnected(false);
    };

    source.onmessage = (evt: MessageEvent<string>) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(evt.data) as ServerEvent;
      } catch {
        // Ignore malformed frames rather than tearing down the stream.
        return;
      }
      handleEvent(event);
    };

    function handleEvent(event: ServerEvent) {
      switch (event.type) {
        case "quote_snapshot":
          seedFromSnapshot(event.quotes ?? []);
          break;
        case "quote_update":
          applyDelta(event.changes ?? []);
          break;
        case "ai_notification":
          setNotifications((prev) => [
            ...prev,
            {
              id: nextEntryId("notif"),
              message: event.message,
              createdAt: Date.now(),
            },
          ]);
          break;
        case "booking_update":
          setBooking({
            booking_id: event.booking_id,
            status: event.status,
            updatedAt: Date.now(),
          });
          break;
        case "ride_status":
          setRideStatuses((prev) => [
            ...prev,
            {
              id: nextEntryId("status"),
              booking_id: event.booking_id,
              message: event.message,
              createdAt: Date.now(),
            },
          ]);
          break;
        // token / session_created / done are handled by the POST stream in
        // useChat; ignore them here to keep a single source of truth.
        default:
          break;
      }
    }

    return () => {
      source.close();
      setConnected(false);
    };
    // Re-open only when the target stream changes. The merge helpers close over
    // refs/state setters, which are stable for the component's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, chatSessionId]);

  // Tell the backend to stop monitoring this session when the user leaves the
  // page (refresh / close / navigation). navigator.sendBeacon is built for
  // unload-time requests: it's fire-and-forget and not cancelled by the page
  // going away. The backend cancels the linked quote session so the monitoring
  // worker stops refreshing it.
  useEffect(() => {
    if (!chatSessionId) {
      return;
    }
    const stopMonitoring = () => {
      try {
        navigator.sendBeacon?.(sessionStopUrl(userId, chatSessionId));
      } catch {
        // Best-effort only; nothing we can do during unload.
      }
    };
    window.addEventListener("pagehide", stopMonitoring);
    return () => window.removeEventListener("pagehide", stopMonitoring);
  }, [userId, chatSessionId]);

  const dismissNotification = (id: string) => {
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  };

  return {
    rideCards,
    notifications,
    rideStatuses,
    booking,
    connected,
    dismissNotification,
  };
}

/** Provider display order for grouped rendering; unknowns sort last. */
const PROVIDER_ORDER: Record<string, number> = {
  uber: 0,
  lyft: 1,
  waymo: 2,
};

/**
 * Stable card ordering: by provider order, then cheapest first, then ride type.
 * Keeps the grid from reshuffling on every silent price tick.
 */
function sortCards(cards: RideCard[]): RideCard[] {
  return [...cards].sort((a, b) => {
    const pa = PROVIDER_ORDER[a.provider] ?? 99;
    const pb = PROVIDER_ORDER[b.provider] ?? 99;
    if (pa !== pb) {
      return pa - pb || a.provider.localeCompare(b.provider);
    }
    const priceA = a.price ?? Number.POSITIVE_INFINITY;
    const priceB = b.price ?? Number.POSITIVE_INFINITY;
    if (priceA !== priceB) {
      return priceA - priceB;
    }
    return a.ride_type.localeCompare(b.ride_type);
  });
}

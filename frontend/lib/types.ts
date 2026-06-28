/**
 * Shared types for the chat domain and the AI Service SSE wire format.
 *
 * The server-pushed event shapes mirror design section 9. Task 7.1 only needs
 * `session_created`, `token`, and `done`, but the full union is declared here so
 * tasks 7.2 (quote/booking updates) and 7.3 (voice) can extend handling without
 * reshaping the types.
 */

/** Who authored a chat message. */
export type ChatRole = "user" | "assistant";

/** A single message rendered in the chat transcript. */
export interface ChatMessage {
  /** Client-side stable id (the server does not return per-token message ids). */
  id: string;
  role: ChatRole;
  content: string;
  /** True while assistant tokens are still streaming into this message. */
  streaming?: boolean;
}

/** Optional GPS coordinate the user can attach as their pickup. */
export interface Location {
  lat: number;
  lng: number;
}

/** Request body for POST /api/chat/message. */
export interface ChatMessageRequest {
  user_id: string;
  /** null on the first message of a conversation; the id thereafter. */
  chat_session_id: string | null;
  message: string;
  location?: Location | null;
}

// --- Quote domain shapes ----------------------------------------------------

/**
 * The three mock providers. Events use lowercase provider keys; we keep the
 * union open to `string` at the wire boundary (see `Quote.provider`) so an
 * unexpected provider can never crash parsing, while this type drives ordering
 * and labelling in the UI.
 */
export type Provider = "uber" | "lyft" | "waymo";

/**
 * A normalized quote as seeded by a `quote_snapshot` event. Mirrors the Quote
 * Service `QuoteOut` shape (design section 10): provider, ride type, price,
 * currency, pickup ETA, trip duration, availability.
 */
export interface Quote {
  /** Stored quote id, when the snapshot carries one. */
  id?: string;
  provider: string;
  ride_type: string;
  price: number;
  currency: string;
  pickup_eta_minutes: number | null;
  trip_duration_minutes: number | null;
  available: boolean;
}

/**
 * One changed ride option carried by a `quote_update` event. A quote_update
 * carries ONLY the options that changed, keyed by (provider, ride_type). A null
 * `new_price` means the option disappeared / became unavailable; a null
 * `old_price` means it newly appeared (design section 8).
 */
export interface QuoteDeltaChange {
  provider: string;
  ride_type: string;
  old_price: number | null;
  new_price: number | null;
  old_pickup_eta_minutes: number | null;
  new_pickup_eta_minutes: number | null;
}

// --- Server-pushed SSE event types (design section 9) -----------------------

export interface SessionCreatedEvent {
  type: "session_created";
  chat_session_id: string;
}

export interface TokenEvent {
  type: "token";
  content: string;
}

export interface DoneEvent {
  type: "done";
}

export interface QuoteSnapshotEvent {
  type: "quote_snapshot";
  /** The full set of normalized quotes used to seed the ride cards. */
  quotes: Quote[];
}

export interface QuoteUpdateEvent {
  type: "quote_update";
  /** Only the options that changed since the last snapshot/update. */
  changes: QuoteDeltaChange[];
}

export interface AiNotificationEvent {
  type: "ai_notification";
  message: string;
}

export interface BookingUpdateEvent {
  type: "booking_update";
  booking_id: string;
  status: string;
}

export interface RideStatusEvent {
  type: "ride_status";
  booking_id: string;
  message: string;
}

/** The full union of events that can arrive on either SSE stream. */
export type ServerEvent =
  | SessionCreatedEvent
  | TokenEvent
  | DoneEvent
  | QuoteSnapshotEvent
  | QuoteUpdateEvent
  | AiNotificationEvent
  | BookingUpdateEvent
  | RideStatusEvent;

// --- Derived UI state (built from the events above) -------------------------

/**
 * A single ride option as held in the live ride-card state. Seeded from a
 * `quote_snapshot` quote and mutated in place by `quote_update` deltas keyed by
 * (provider, ride_type). `updatedAt` is bumped on every delta so the card can
 * flash a subtle highlight (Requirement 3.3 silent updates).
 */
export interface RideCard {
  provider: string;
  ride_type: string;
  /** Latest price, or null when the option has disappeared. */
  price: number | null;
  currency: string;
  pickup_eta_minutes: number | null;
  trip_duration_minutes: number | null;
  available: boolean;
  /** Epoch ms of the last change applied to this card. */
  updatedAt: number;
}

/** An AI notification surfaced as a transient banner (Requirement 3.4). */
export interface NotificationEntry {
  id: string;
  message: string;
  createdAt: number;
}

/** A ride milestone message for the status timeline (Requirement 6.3). */
export interface RideStatusEntry {
  id: string;
  booking_id: string;
  message: string;
  createdAt: number;
}

/** The current booking lifecycle state reflected in the UI. */
export interface BookingState {
  booking_id: string;
  status: string;
  updatedAt: number;
}

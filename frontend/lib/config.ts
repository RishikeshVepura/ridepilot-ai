/**
 * Runtime configuration for talking to the AI Service.
 *
 * Per design rule 1, the frontend only ever talks to the AI Service. Its base
 * URL is configurable via the NEXT_PUBLIC_AI_SERVICE_URL env var so the same
 * build can point at different environments, falling back to the local Docker
 * mapping (http://localhost:8001) used in development.
 */

/** Base URL of the AI Service, without a trailing slash. */
export const AI_SERVICE_URL = (
  process.env.NEXT_PUBLIC_AI_SERVICE_URL ?? "http://localhost:8001"
).replace(/\/$/, "");

/**
 * Identifier for the current user. The MVP runs as a single demo user; this is
 * overridable via env so multiple users could be simulated later.
 */
export const USER_ID = process.env.NEXT_PUBLIC_USER_ID ?? "demo_user";

/** Absolute URL for the chat message endpoint (POST, SSE response). */
export const chatMessageUrl = (): string => `${AI_SERVICE_URL}/api/chat/message`;

/**
 * Absolute URL for the per-session server-push SSE stream (GET). Used by later
 * tasks (7.2) for live quote/booking updates; provided here so the streaming
 * groundwork is in one place.
 */
export const sessionStreamUrl = (
  userId: string,
  chatSessionId: string,
): string =>
  `${AI_SERVICE_URL}/api/stream/${encodeURIComponent(
    userId,
  )}/${encodeURIComponent(chatSessionId)}`;

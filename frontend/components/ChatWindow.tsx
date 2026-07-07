"use client";

/**
 * ChatWindow — the top-level surface that brings chat, live ride cards, and
 * booking/ride status together.
 *
 * State ownership:
 *   - useChat owns the transcript and the POST /api/chat/message turn (token
 *     streaming for the assistant reply).
 *   - useSessionStream owns the GET server-push stream and is the single source
 *     of truth for non-token state: ride cards (quote_snapshot / quote_update),
 *     AI notifications, and booking/ride status. It opens automatically once
 *     useChat has a chat_session_id. See useSessionStream for why these are kept
 *     on separate paths (avoids double-applying events that can arrive on both
 *     streams).
 *
 * Voice (Requirement 8.2): ChatWindow is where text-to-speech is wired, because
 * it observes exactly the things worth speaking:
 *   - an assistant reply, once it finishes streaming (streaming true → false);
 *   - each new `ai_notification` banner (the meaningful price/ETA changes from
 *     Requirement 3.4); and
 *   - each new `ride_status` milestone (Requirement 6.3).
 * We speak the final, fully-assembled text per message — never partial tokens —
 * and dedupe by message id so nothing is spoken twice. Voice is off by default
 * and gated behind the header toggle (which doubles as the user gesture
 * browsers require before audio can play).
 */

import { useEffect, useRef } from "react";

import { useChat } from "@/hooks/useChat";
import { useSessionStream } from "@/hooks/useSessionStream";
import { useSpeechSynthesis } from "@/hooks/useSpeechSynthesis";
import { ChatInput } from "./ChatInput";
import { MessageList } from "./MessageList";
import { NotificationBanner } from "./NotificationBanner";
import { RideCards } from "./RideCards";
import { RideStatusPanel } from "./RideStatusPanel";
import { RouteMap } from "./RouteMap";

export function ChatWindow() {
  const { messages, isStreaming, chatSessionId, userId, sendMessage, error, appendAssistantMessage } =
    useChat();

  const {
    rideCards,
    notifications,
    rideStatuses,
    booking,
    route,
    isBooked,
    dismissNotification,
  } = useSessionStream(userId, chatSessionId);

  const {
    isSupported: voiceSupported,
    enabled: voiceEnabled,
    toggle: toggleVoice,
    speak,
  } = useSpeechSynthesis();

  // Ids we've already handed to TTS, so re-renders never re-speak. Items are
  // recorded here even while voice is OFF (speak() no-ops), which means turning
  // voice ON only speaks things that happen AFTERWARD — never the backlog.
  const spokenRef = useRef<Set<string>>(new Set());

  // Speak each assistant reply once it has finished streaming.
  useEffect(() => {
    for (const m of messages) {
      if (
        m.role === "assistant" &&
        m.streaming === false &&
        m.content.trim().length > 0 &&
        !spokenRef.current.has(m.id)
      ) {
        spokenRef.current.add(m.id);
        speak(m.content);
      }
    }
  }, [messages, speak]);

  // Speak each new AI notification (meaningful price/ETA change).
  useEffect(() => {
    for (const n of notifications) {
      if (!spokenRef.current.has(n.id)) {
        spokenRef.current.add(n.id);
        speak(n.message);
      }
    }
  }, [notifications, speak]);

  // Also surface each AI notification inline in the chat transcript (not just as
  // a transient banner). Deduped by id inside appendAssistantMessage, so the
  // banner auto-dismissing later never removes the chat copy.
  useEffect(() => {
    for (const n of notifications) {
      appendAssistantMessage(`notif_${n.id}`, n.message);
    }
  }, [notifications, appendAssistantMessage]);

  // Speak each new ride milestone.
  useEffect(() => {
    for (const s of rideStatuses) {
      if (!spokenRef.current.has(s.id)) {
        spokenRef.current.add(s.id);
        speak(s.message);
      }
    }
  }, [rideStatuses, speak]);

  const hasUpdates =
    rideCards.length > 0 ||
    booking !== null ||
    rideStatuses.length > 0 ||
    notifications.length > 0 ||
    route !== null;

  return (
    <div className="app">
      <section className="chat-pane">
        <header className="chat__header">
          <div className="chat__brand">
            <span className="chat__logo" aria-hidden="true">
              🚗
            </span>
            <div>
              <h1 className="chat__title">RidePilot AI</h1>
              <p className="chat__subtitle">
                Your AI ride assistant
                {chatSessionId ? (
                  <span className="chat__session"> · session active</span>
                ) : null}
              </p>
            </div>
          </div>

          {voiceSupported ? (
            <button
              type="button"
              className={`chat__voice-toggle${
                voiceEnabled ? " chat__voice-toggle--on" : ""
              }`}
              onClick={toggleVoice}
              aria-pressed={voiceEnabled}
              aria-label={
                voiceEnabled
                  ? "Turn voice responses off"
                  : "Turn voice responses on"
              }
              title={
                voiceEnabled
                  ? "Voice responses on — click to mute"
                  : "Voice responses off — click to hear replies"
              }
            >
              <span aria-hidden="true">{voiceEnabled ? "🔊" : "🔇"}</span>
              <span className="chat__voice-label">
                {voiceEnabled ? "Voice on" : "Voice off"}
              </span>
            </button>
          ) : null}
        </header>

        <MessageList messages={messages} />

        {error ? (
          <div className="chat__error" role="alert">
            {error}
          </div>
        ) : null}

        <ChatInput
          onSend={(text) => void sendMessage(text)}
          disabled={isStreaming}
        />
      </section>

      <aside
        className={`side-pane${hasUpdates ? " side-pane--visible" : ""}`}
        aria-hidden={!hasUpdates}
      >
        <div className="side-pane__title">
          {isBooked ? "Your ride" : "Ride options"}
        </div>

        <NotificationBanner
          notifications={notifications}
          onDismiss={dismissNotification}
        />

        <div className="side-pane__content">
          <RouteMap route={route} />
          {/* Ride cards are only useful while comparing options.
              Once a booking is created they fade out — only the map stays. */}
          {!isBooked && <RideCards cards={rideCards} />}
          <RideStatusPanel booking={booking} rideStatuses={rideStatuses} />
        </div>
      </aside>
    </div>
  );
}

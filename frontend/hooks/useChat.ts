"use client";

/**
 * useChat — owns the chat conversation state and the POST/stream logic for
 * talking to the AI Service.
 *
 * Responsibilities (task 7.1):
 *   - Hold the transcript of user + assistant messages.
 *   - Track the chat_session_id: null until the first response's
 *     `session_created` event, then reused on every subsequent message so the
 *     backend keeps the same conversation (design section 7).
 *   - POST a message and read the SSE response body incrementally, appending
 *     `token` content to a live assistant message as it streams in, which is
 *     how the AI's cheapest/fastest summary surfaces (Requirement 2.4).
 *
 * It is deliberately decoupled from rendering so tasks 7.2 (ride cards + the GET
 * server-push stream) and 7.3 (voice) can build on the same state: the exposed
 * `userId` / `chatSessionId` are exactly what the GET stream URL needs, and an
 * `onEvent` callback surfaces every parsed server event for future handlers.
 */

import { useCallback, useRef, useState } from "react";

import { USER_ID, chatMessageUrl } from "@/lib/config";
import { parseSSEStream } from "@/lib/sse";
import type {
  ChatMessage,
  ChatMessageRequest,
  Location,
  ServerEvent,
} from "@/lib/types";

let messageCounter = 0;
/** Generate a process-unique id for a transcript message. */
function nextMessageId(prefix: string): string {
  messageCounter += 1;
  return `${prefix}_${Date.now()}_${messageCounter}`;
}

export interface UseChatOptions {
  /**
   * Optional hook into every parsed server event (tokens included). Tasks 7.2 /
   * 7.3 use this to react to quote/booking/notification events and to drive
   * text-to-speech; task 7.1 does not require it.
   */
  onEvent?: (event: ServerEvent) => void;
}

export interface UseChatResult {
  messages: ChatMessage[];
  /** True while a send is in flight (request open or tokens streaming). */
  isStreaming: boolean;
  /** The current chat session id, or null before the first reply. */
  chatSessionId: string | null;
  userId: string;
  /** Last error message, or null. */
  error: string | null;
  /** Send a user message and stream the assistant reply. */
  sendMessage: (text: string, location?: Location | null) => Promise<void>;
}

export function useChat(options: UseChatOptions = {}): UseChatResult {
  const { onEvent } = options;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Keep the session id in a ref too so concurrent sends read the freshest value
  // without waiting for a re-render.
  const chatSessionIdRef = useRef<string | null>(null);

  /** Append text to a specific assistant message as tokens arrive. */
  const appendToAssistant = useCallback(
    (assistantId: string, chunk: string) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, content: m.content + chunk } : m,
        ),
      );
    },
    [],
  );

  /** Mark an assistant message as no longer streaming. */
  const finalizeAssistant = useCallback((assistantId: string) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === assistantId ? { ...m, streaming: false } : m,
      ),
    );
  }, []);

  const sendMessage = useCallback(
    async (text: string, location?: Location | null) => {
      const trimmed = text.trim();
      if (trimmed.length === 0 || isStreaming) {
        return;
      }

      setError(null);
      setIsStreaming(true);

      const userMessage: ChatMessage = {
        id: nextMessageId("user"),
        role: "user",
        content: trimmed,
      };
      const assistantId = nextMessageId("assistant");
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        streaming: true,
      };
      setMessages((prev) => [...prev, userMessage, assistantMessage]);

      const body: ChatMessageRequest = {
        user_id: USER_ID,
        chat_session_id: chatSessionIdRef.current,
        message: trimmed,
        location: location ?? null,
      };

      try {
        const response = await fetch(chatMessageUrl(), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
          },
          body: JSON.stringify(body),
        });

        if (!response.ok || !response.body) {
          throw new Error(
            `AI Service responded with ${response.status} ${response.statusText}`,
          );
        }

        for await (const event of parseSSEStream<ServerEvent>(response.body)) {
          onEvent?.(event);

          switch (event.type) {
            case "session_created": {
              chatSessionIdRef.current = event.chat_session_id;
              setChatSessionId(event.chat_session_id);
              break;
            }
            case "token": {
              appendToAssistant(assistantId, event.content);
              break;
            }
            case "done": {
              // Stream for this turn is complete.
              break;
            }
            default:
              // Other event types (quote_update, ai_notification, etc.) can
              // arrive here once the backend interleaves them; tasks 7.2/7.3
              // handle them via onEvent. No transcript change needed in 7.1.
              break;
          }
        }
      } catch (err) {
        const messageText =
          err instanceof Error ? err.message : "Failed to reach the AI Service.";
        setError(messageText);
        // Surface the failure inline so the user is not left with an empty
        // assistant bubble.
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content:
                    m.content.length > 0
                      ? m.content
                      : `⚠️ ${messageText}`,
                  streaming: false,
                }
              : m,
          ),
        );
      } finally {
        finalizeAssistant(assistantId);
        setIsStreaming(false);
      }
    },
    [appendToAssistant, finalizeAssistant, isStreaming, onEvent],
  );

  return {
    messages,
    isStreaming,
    chatSessionId,
    userId: USER_ID,
    error,
    sendMessage,
  };
}

"use client";

import { useEffect, useRef } from "react";

import type { ChatMessage } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";

/**
 * The scrollable transcript. Auto-scrolls to the newest content as messages
 * arrive or stream in, and shows an empty-state prompt before the first message.
 */
export function MessageList({ messages }: { messages: ChatMessage[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keep the latest tokens in view as the assistant streams.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="message-list message-list--empty">
        <div className="empty-state">
          <h2>Where would you like to go?</h2>
          <p>
            Ask for a ride in plain language, e.g.{" "}
            <em>&ldquo;Find me a ride to the airport&rdquo;</em>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

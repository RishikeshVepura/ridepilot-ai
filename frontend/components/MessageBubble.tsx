import type { ChatMessage } from "@/lib/types";

/**
 * A single chat bubble. User messages align right, assistant messages left.
 * While an assistant message is still streaming it shows a blinking caret so the
 * live token append is visually obvious.
 */
export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const showCaret = message.streaming;
  const isEmptyStreaming = showCaret && message.content.length === 0;

  return (
    <div className={`bubble-row ${isUser ? "bubble-row--user" : "bubble-row--assistant"}`}>
      <div className={`bubble ${isUser ? "bubble--user" : "bubble--assistant"}`}>
        {isEmptyStreaming ? (
          <span className="typing" aria-label="Assistant is typing">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </span>
        ) : (
          <span className="bubble__text">
            {message.content}
            {showCaret ? <span className="caret" aria-hidden="true" /> : null}
          </span>
        )}
      </div>
    </div>
  );
}

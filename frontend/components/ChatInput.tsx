"use client";

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { useSpeechRecognition } from "@/hooks/useSpeechRecognition";

/**
 * The message composer: a growing textarea, a mic button for voice input, and a
 * send button. Submits on Enter (Shift+Enter inserts a newline) and is disabled
 * while a reply is streaming so the user cannot interleave turns.
 *
 * Voice input (Requirement 8.1): the mic toggles browser speech-to-text. While
 * listening, the recognized words stream live into the textarea. We deliberately
 * do NOT auto-send on the final result — the transcript lands in the input so
 * the user can correct any misrecognition and then press Send (or Enter). This
 * keeps misheard words from being fired off as a turn. The text is sent through
 * the SAME `onSend` path as typing, so the backend can't tell voice from typing
 * (Requirement 8.3).
 *
 * Graceful degradation (Requirement 8.1): when the browser has no speech engine,
 * the mic button is hidden entirely and typing works exactly as before. If the
 * user denies microphone permission, a small hint is shown and typing still
 * works.
 */
export function ChatInput({
  onSend,
  disabled,
}: {
  onSend: (text: string) => void;
  disabled: boolean;
}) {
  const [value, setValue] = useState("");
  const {
    isSupported: micSupported,
    isListening,
    transcript,
    error: micError,
    start,
    stop,
    reset,
  } = useSpeechRecognition();

  // While listening, mirror the live transcript into the textarea so the user
  // sees words appear as they speak. A ref guards against clobbering manual
  // edits once recognition has ended.
  const listeningRef = useRef(false);
  listeningRef.current = isListening;

  useEffect(() => {
    if (isListening) {
      setValue(transcript);
    }
  }, [transcript, isListening]);

  const submit = () => {
    const trimmed = value.trim();
    if (trimmed.length === 0 || disabled) {
      return;
    }
    if (listeningRef.current) {
      stop();
    }
    onSend(trimmed);
    setValue("");
    reset();
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const toggleMic = () => {
    if (disabled) {
      return;
    }
    if (isListening) {
      stop();
    } else {
      start();
    }
  };

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <textarea
        className="composer__input"
        placeholder={isListening ? "Listening…" : "Message RidePilot…"}
        rows={1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        aria-label="Message"
      />

      {micSupported ? (
        <button
          type="button"
          className={`composer__mic${
            isListening ? " composer__mic--listening" : ""
          }`}
          onClick={toggleMic}
          disabled={disabled}
          aria-pressed={isListening}
          aria-label={isListening ? "Stop voice input" : "Start voice input"}
          title={
            micError === "not-allowed"
              ? "Microphone permission denied"
              : isListening
                ? "Stop voice input"
                : "Speak your message"
          }
        >
          {isListening ? "■" : "🎤"}
        </button>
      ) : null}

      <button
        type="submit"
        className="composer__send"
        disabled={disabled || value.trim().length === 0}
        aria-label="Send message"
      >
        Send
      </button>
    </form>
  );
}

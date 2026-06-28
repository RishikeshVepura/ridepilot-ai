"use client";

/**
 * useSpeechRecognition — a thin, resilient wrapper over the browser Web Speech
 * API (speech-to-text) for Requirement 8.1.
 *
 * The recognized text is plain text: callers feed it straight into the existing
 * `sendMessage` path, so the backend never sees audio and treats spoken and
 * typed input identically (Requirement 8.3). Voice is purely a browser-layer
 * I/O concern here.
 *
 * Design choices:
 *   - Single-utterance mode (`continuous = false`): the user taps the mic,
 *     speaks one request, and recognition ends on a natural pause. This maps
 *     cleanly to "compose one message" and avoids an always-on hot mic.
 *   - `interimResults = true`: interim hypotheses stream into `transcript` so
 *     the composer can show words appearing live; the final result replaces
 *     them once stable.
 *   - Graceful degradation: when the API is absent, `isSupported` is false and
 *     `start()` is a no-op. Typing must always keep working (the caller hides /
 *     disables the mic button accordingly).
 *
 * Permission denial (`not-allowed`) and other engine errors surface via
 * `error`; the hook never throws so it can't break the typed-text flow.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface UseSpeechRecognitionOptions {
  /** BCP-47 language tag. Defaults to the browser's language, then "en-US". */
  lang?: string;
}

export interface UseSpeechRecognitionResult {
  /** True when the browser exposes a SpeechRecognition implementation. */
  isSupported: boolean;
  /** True between a successful `start()` and recognition ending. */
  isListening: boolean;
  /** Best current transcript (final text plus the latest interim hypothesis). */
  transcript: string;
  /** Last error code/message from the engine, or null. */
  error: string | null;
  /** Begin listening. No-op if unsupported or already listening. */
  start: () => void;
  /** Stop listening; any in-flight final result still arrives via `transcript`. */
  stop: () => void;
  /** Clear the current transcript and error (e.g. after the message is sent). */
  reset: () => void;
}

/** Resolve the constructor regardless of vendor prefix; null when unsupported. */
function getRecognitionCtor(): SpeechRecognitionStatic | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

export function useSpeechRecognition(
  options: UseSpeechRecognitionOptions = {},
): UseSpeechRecognitionResult {
  const { lang } = options;

  const [isSupported, setIsSupported] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [error, setError] = useState<string | null>(null);

  // The live recognition instance. Kept in a ref so start/stop don't re-render
  // and so cleanup can always reach the current instance.
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  // Final (stable) text accumulated across result events for this utterance.
  const finalTranscriptRef = useRef("");

  // Feature-detect once on mount (window is only available client-side).
  useEffect(() => {
    setIsSupported(getRecognitionCtor() !== null);
  }, []);

  // Tear down any live recognition when the component unmounts.
  useEffect(() => {
    return () => {
      const recognition = recognitionRef.current;
      if (recognition) {
        recognition.onresult = null;
        recognition.onerror = null;
        recognition.onend = null;
        recognition.onstart = null;
        try {
          recognition.abort();
        } catch {
          // Already stopped; nothing to do.
        }
        recognitionRef.current = null;
      }
    };
  }, []);

  const reset = useCallback(() => {
    finalTranscriptRef.current = "";
    setTranscript("");
    setError(null);
  }, []);

  const start = useCallback(() => {
    const Ctor = getRecognitionCtor();
    if (!Ctor || recognitionRef.current) {
      // Unsupported, or already listening — ignore.
      return;
    }

    const recognition = new Ctor();
    recognition.lang =
      lang ??
      (typeof navigator !== "undefined" ? navigator.language : "") ??
      "en-US";
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    // Fresh utterance: clear any previous text/error.
    finalTranscriptRef.current = "";
    setTranscript("");
    setError(null);

    recognition.onstart = () => {
      setIsListening(true);
    };

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let interim = "";
      // Walk every result; final ones are appended permanently, interim ones
      // are shown transiently after the stable text.
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const text = result[0]?.transcript ?? "";
        if (result.isFinal) {
          finalTranscriptRef.current =
            `${finalTranscriptRef.current} ${text}`.trim();
        } else {
          interim += text;
        }
      }
      const combined = `${finalTranscriptRef.current} ${interim}`.trim();
      setTranscript(combined);
    };

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      // "aborted" / "no-speech" are benign user-flow outcomes, not failures we
      // want to shout about; surface the rest (notably "not-allowed").
      if (event.error !== "aborted" && event.error !== "no-speech") {
        setError(event.error || "speech-recognition-error");
      }
    };

    recognition.onend = () => {
      setIsListening(false);
      recognitionRef.current = null;
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
    } catch (err) {
      // Calling start() twice throws in some engines; recover quietly.
      setError(err instanceof Error ? err.message : "speech-recognition-error");
      setIsListening(false);
      recognitionRef.current = null;
    }
  }, [lang]);

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    if (recognition) {
      try {
        recognition.stop();
      } catch {
        // Already stopped.
      }
    }
  }, []);

  return { isSupported, isListening, transcript, error, start, stop, reset };
}

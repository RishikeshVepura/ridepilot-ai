"use client";

/**
 * useSpeechSynthesis — a thin wrapper over the browser SpeechSynthesis API
 * (text-to-speech) for Requirement 8.2: reading the AI's replies and spoken
 * notifications aloud.
 *
 * Design choices:
 *   - Off by default. Browsers gate audio behind a user gesture, and silent
 *     autoplay on first load is surprising. The user flips voice ON via a
 *     toggle (which is itself the required gesture) and can mute any time.
 *   - `speak(text)` is a no-op unless voice is both supported AND enabled, so
 *     callers can fire it freely as messages complete without guarding.
 *   - Each new utterance cancels the one in progress, and toggling OFF cancels
 *     immediately — the assistant never talks over itself or keeps talking
 *     after being muted.
 *
 * TTS types (`speechSynthesis`, `SpeechSynthesisUtterance`) are part of the
 * standard DOM lib, so no ambient declarations are needed here.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface UseSpeechSynthesisResult {
  /** True when the browser exposes speechSynthesis. */
  isSupported: boolean;
  /** Whether the user has voice output switched on. */
  enabled: boolean;
  /** True while an utterance is actively being spoken. */
  isSpeaking: boolean;
  /** Flip voice on/off. Turning off cancels any in-progress speech. */
  toggle: () => void;
  /** Speak `text`. No-op when unsupported, disabled, or text is empty. */
  speak: (text: string) => void;
  /** Stop any in-progress speech immediately. */
  cancel: () => void;
}

export interface UseSpeechSynthesisOptions {
  /** BCP-47 language tag for the utterance. Defaults to the browser language. */
  lang?: string;
}

export function useSpeechSynthesis(
  options: UseSpeechSynthesisOptions = {},
): UseSpeechSynthesisResult {
  const { lang } = options;

  const [isSupported, setIsSupported] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);

  // Mirror `enabled` in a ref so the stable `speak` callback always reads the
  // current value without being re-created (and without callers re-binding).
  const enabledRef = useRef(false);

  useEffect(() => {
    setIsSupported(
      typeof window !== "undefined" && "speechSynthesis" in window,
    );
  }, []);

  const cancel = useCallback(() => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    setIsSpeaking(false);
  }, []);

  const speak = useCallback(
    (text: string) => {
      if (
        !enabledRef.current ||
        typeof window === "undefined" ||
        !("speechSynthesis" in window)
      ) {
        return;
      }
      const trimmed = text.trim();
      if (trimmed.length === 0) {
        return;
      }

      // Never let two utterances overlap.
      window.speechSynthesis.cancel();

      const utterance = new SpeechSynthesisUtterance(trimmed);
      utterance.lang =
        lang ??
        (typeof navigator !== "undefined" ? navigator.language : "") ??
        "en-US";
      utterance.onstart = () => setIsSpeaking(true);
      utterance.onend = () => setIsSpeaking(false);
      utterance.onerror = () => setIsSpeaking(false);

      window.speechSynthesis.speak(utterance);
    },
    [lang],
  );

  const toggle = useCallback(() => {
    setEnabled((prev) => {
      const next = !prev;
      enabledRef.current = next;
      if (!next) {
        // Muting: stop talking right away.
        if (typeof window !== "undefined" && "speechSynthesis" in window) {
          window.speechSynthesis.cancel();
        }
        setIsSpeaking(false);
      }
      return next;
    });
  }, []);

  // Safety net: cancel any queued speech if the component unmounts.
  useEffect(() => {
    return () => {
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  return { isSupported, enabled, isSpeaking, toggle, speak, cancel };
}

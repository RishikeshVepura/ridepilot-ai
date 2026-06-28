/**
 * Ambient type declarations for the browser Web Speech API (speech-to-text).
 *
 * The standard TypeScript DOM lib ships types for SpeechSynthesis (TTS —
 * `window.speechSynthesis`, `SpeechSynthesisUtterance`) but NOT for
 * SpeechRecognition (STT). Chrome/Edge expose it as `webkitSpeechRecognition`
 * (and, increasingly, the unprefixed `SpeechRecognition`). We declare just
 * enough of the surface we use in `useSpeechRecognition` so the rest of the app
 * stays strictly typed.
 *
 * This file is picked up automatically by tsconfig's `"**\/*.ts"` include — no
 * config change required.
 */

interface SpeechRecognitionAlternative {
  readonly transcript: string;
  readonly confidence: number;
}

interface SpeechRecognitionResult {
  readonly isFinal: boolean;
  readonly length: number;
  item(index: number): SpeechRecognitionAlternative;
  [index: number]: SpeechRecognitionAlternative;
}

interface SpeechRecognitionResultList {
  readonly length: number;
  item(index: number): SpeechRecognitionResult;
  [index: number]: SpeechRecognitionResult;
}

interface SpeechRecognitionEvent extends Event {
  readonly resultIndex: number;
  readonly results: SpeechRecognitionResultList;
}

/**
 * The `error` field is a short machine code such as `"no-speech"`,
 * `"not-allowed"` (permission denied), `"network"`, or `"aborted"`.
 */
interface SpeechRecognitionErrorEvent extends Event {
  readonly error: string;
  readonly message: string;
}

interface SpeechRecognition extends EventTarget {
  /** BCP-47 language tag, e.g. "en-US". */
  lang: string;
  /** Keep recognizing across pauses when true. We use single-utterance mode. */
  continuous: boolean;
  /** Emit interim (non-final) hypotheses so the input can update live. */
  interimResults: boolean;
  maxAlternatives: number;

  start(): void;
  stop(): void;
  abort(): void;

  onresult:
    | ((this: SpeechRecognition, ev: SpeechRecognitionEvent) => unknown)
    | null;
  onerror:
    | ((this: SpeechRecognition, ev: SpeechRecognitionErrorEvent) => unknown)
    | null;
  onend: ((this: SpeechRecognition, ev: Event) => unknown) | null;
  onstart: ((this: SpeechRecognition, ev: Event) => unknown) | null;
  onspeechend: ((this: SpeechRecognition, ev: Event) => unknown) | null;
}

interface SpeechRecognitionStatic {
  prototype: SpeechRecognition;
  new (): SpeechRecognition;
}

declare var SpeechRecognition: SpeechRecognitionStatic;
declare var webkitSpeechRecognition: SpeechRecognitionStatic;

interface Window {
  SpeechRecognition?: SpeechRecognitionStatic;
  webkitSpeechRecognition?: SpeechRecognitionStatic;
}

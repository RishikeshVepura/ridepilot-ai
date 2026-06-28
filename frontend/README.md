# RidePilot AI — Frontend

Next.js (App Router) + TypeScript chat interface for the RidePilot AI assistant.
Per the system design, the frontend talks to **only** the AI Service.

## What's here (task 7.1)

- A chat UI: message transcript (user + assistant bubbles) and a composer.
- `POST /api/chat/message` to the AI Service, with the SSE response body parsed
  incrementally so assistant tokens render live as they stream in.
- The `chat_session_id` is captured from the first response's `session_created`
  event and reused on every later message.

Tasks 7.2 (ride cards + the `GET /api/stream/...` server-push connection) and
7.3 (voice STT/TTS) build on this structure — see `hooks/useChat.ts` (exposes
`userId` / `chatSessionId` and an `onEvent` callback) and `lib/config.ts`
(`sessionStreamUrl`).

## Configuration

Copy `.env.local.example` to `.env.local` and adjust if needed:

| Variable                     | Default                 | Purpose                          |
| ---------------------------- | ----------------------- | -------------------------------- |
| `NEXT_PUBLIC_AI_SERVICE_URL` | `http://localhost:8001` | Base URL of the AI Service       |
| `NEXT_PUBLIC_USER_ID`        | `demo_user`             | Identifier for the current user  |

## Project layout

```
app/            App Router entry (layout, page, global styles)
components/      Presentational chat components
hooks/           useChat — POST + SSE streaming + conversation state
lib/             config, shared types, SSE stream parser
```

## Running

This repo runs in Docker; container lifecycle is owned by the dev scripts. The
standard Next.js scripts are available once dependencies are installed:

- `npm run dev` — start the dev server on port 3000
- `npm run build` / `npm run start` — production build and serve

The AI Service must be reachable at `NEXT_PUBLIC_AI_SERVICE_URL` for chat to work.

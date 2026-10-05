# Langfuse observability

This document describes RidePilot's Langfuse implementation: what is traced,
how observations are nested, how the self-hosted endpoint is configured, why
the LiteLLM streaming settings are required, and how tracing behaves when
Langfuse is unavailable.

## Goals and scope

The integration answers four debugging questions for every live AI turn:

1. What input did the model receive?
2. What did each model invocation return?
3. Which backend tools did the model request, and what arguments did RidePilot
   actually execute?
4. What result did each tool return before the next model invocation?

It traces the AI Service's agent loop. It does not automatically trace database
queries, browser activity, background quote refreshes, booking-status workers,
or every internal HTTP request. Tool observations contain the useful boundary
data for calls from the agent to the quote and booking services without adding
low-level transport noise.

## Trace structure

One user message creates one trace. The chat session ID groups those per-turn
traces into a Langfuse session.

```text
ridepilot.ai-turn                         agent; one user turn
├── generate-response                    generation; model round 1
├── create-quote-session                 tool
├── generate-response                    generation; model round 2
├── fetch-quotes                         tool
└── generate-response                    generation; final model round
```

Generations and tools are siblings under the agent that orchestrates them. A
loop with more or fewer tool calls naturally has a different number of children.
Every model invocation remains a separate generation so its prompt, decision,
latency, tokens, and cost can be inspected independently.

### Agent observation

`ridepilot.ai-turn` is a typed `agent` observation covering the lifetime of the
streamed response.

| Field | Value |
|---|---|
| Input | The current user message as a string |
| Output | The complete assistant text accumulated from streamed chunks |
| Metadata | Provider, model, chat-history message count, and loop round count |
| User | RidePilot user ID |
| Session | Chat session ID |
| Tags | `ridepilot`, `tool-calling`, and provider |

The context manager is inside the async generator. This is important: the agent
observation stays active while the caller consumes the stream instead of ending
when the generator object is created.

### LLM generation observations

LiteLLM's `langfuse_otel` callback creates one typed generation for each call to
the provider. Every generation uses the stable name `generate-response`.
Run-specific data such as `round` is metadata rather than part of the name; this
keeps dashboards, filters, and evaluators stable.

The integration records the message list sent to the model, the assistant
message or requested tool calls returned by the model, model name, latency,
input/output token usage, and calculated cost. Tool schemas and request settings
are also available through the LiteLLM generation attributes.

The first generation normally sees the system prompt, authoritative ride-state
block, recent conversation, and current user message. Later generations also
see assistant tool requests and the corresponding tool results.

### Tool observations

Each executed backend tool is a typed `tool` observation. Its stable name is the
action name with underscores converted to hyphens, for example
`create-quote-session`, `fetch-quotes`, or `verify-booking`.

- Input is the trusted, resolved argument dictionary RidePilot executes. This
  may include IDs injected by the backend rather than accepted from the model.
- Output is the exact result envelope supplied to the next LLM round.
- A result with `success: false` is marked `ERROR`, and its error text becomes
  the observation status message.
- Unknown tools are recorded as `reject-unknown-tool`.

This distinction makes it possible to compare what the model requested with
what the trusted backend actually executed.

## Implementation map

| File | Responsibility |
|---|---|
| `services/ai-service/core/obs.py` | Detects configuration, registers the LiteLLM callback, initializes the Langfuse client, and shuts it down cleanly. |
| `services/ai-service/main.py` | Calls Langfuse setup and shutdown from the FastAPI lifespan. |
| `services/ai-service/services/llm_client.py` | Passes observability metadata into every LiteLLM request. |
| `services/ai-service/services/llm_service.py` | Creates the root agent observation, propagates user/session attributes, and traces tool execution. |
| `services/ai-service/requirements.txt` | Declares Langfuse, OpenTelemetry, OTLP exporter, and callback configuration dependencies. |

The integration is hybrid by design:

- Manual Langfuse observations define RidePilot-specific agent and tool
  boundaries with clean inputs and outputs.
- LiteLLM's integration automatically records provider-specific generations,
  token usage, model information, cost, and streamed results.

## Dependencies

The AI Service installs:

```text
litellm==1.90.2
langfuse>=4.7.0,<5.0.0
opentelemetry-api>=1.25.0,<2.0.0
opentelemetry-sdk>=1.25.0,<2.0.0
opentelemetry-exporter-otlp>=1.25.0,<2.0.0
pydantic-settings>=2.7.0,<3.0.0
```

`pydantic-settings` is a separate package from `pydantic`. LiteLLM's
OpenTelemetry integration imports it while constructing the `langfuse_otel`
logger. If it is absent, LiteLLM reports a non-blocking custom-logger error and
automatic generation tracing does not initialize correctly.

Changing Python dependencies requires rebuilding the AI Service image:

```bash
docker compose up -d --build --no-deps ai-service
```

## Configuration

Use project-specific API keys created in the self-hosted Langfuse project.
Never commit actual keys.

```env
# Langfuse project credentials. Leave both blank to disable remote tracing.
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=

# Origin of the self-hosted Langfuse deployment as seen from the AI container.
LANGFUSE_BASE_URL=http://host.docker.internal:3001
LANGFUSE_OTEL_HOST=http://host.docker.internal:3001

# Keep SDK and OpenTelemetry observations in the same environment.
LANGFUSE_TRACING_ENVIRONMENT=development
OTEL_ENVIRONMENT_NAME=development

# Give every streamed LiteLLM call its own generation span.
USE_OTEL_LITELLM_REQUEST_SPAN=true

# Suppress LiteLLM's incomplete secondary raw-stream span. The primary
# Langfuse generation still records messages and the completed response.
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
```

`host.docker.internal` is required when Langfuse is published on the host and
the AI Service runs in Docker Desktop. If both applications share a Compose
network, use the Langfuse service name and container port instead.

`LANGFUSE_OTEL_HOST` must be only the origin. Do not append
`/api/public/otel`: LiteLLM adds that base path and the OTLP exporter adds
`/v1/traces`, producing the final ingestion request:

```text
POST http://host.docker.internal:3001/api/public/otel/v1/traces
```

RidePilot sends OTLP/HTTP directly to Langfuse. It does not require a separate
OpenTelemetry Collector.

Environment-only changes require container recreation, not an image rebuild:

```bash
docker compose up -d --force-recreate --no-deps ai-service
```

## Why the streaming settings are required

LiteLLM 1.90.2 normally reuses an active parent OpenTelemetry span instead of
creating a dedicated request span. For a streamed response, its success callback
can run after `ridepilot.ai-turn` has closed. LiteLLM then attempts to put the
model input/output on an ended span, and OpenTelemetry discards those writes:

```text
Tried calling set_status on an ended span.
Setting attribute on ended span.
```

The secondary raw-stream span may still appear as `generate-response`, but it
contains only the first stream marker and therefore renders as `null` input and
`undefined` output.

`USE_OTEL_LITELLM_REQUEST_SPAN=true` forces a dedicated generation span that can
be completed with the assembled response. The capture-mode setting removes the
incomplete duplicate raw span. Existing broken observations are immutable; only
new turns reflect the corrected configuration.

## Startup, export, and shutdown

During FastAPI startup, `configure_langfuse()` checks that both project keys are
present. If they are, it registers `langfuse_otel` in LiteLLM's callbacks and
initializes the environment-configured Langfuse singleton.

The SDK and OTLP exporter batch observations in memory and send them in the
background so telemetry does not add network latency to each streamed token or
tool call.

During graceful FastAPI shutdown, RidePilot calls:

```python
get_client().shutdown()
```

Shutdown sends queued observations, waits briefly for the exporter, and closes
its background workers. This is commonly described as *flushing pending
observations*. It does not delete or clear data in Langfuse. Without it, traces
created immediately before a container restart could remain only in memory and
be lost when the process exits.

## Behavior when Langfuse is unavailable

Langfuse is optional and must not be a prerequisite for chat.

- If either project key is missing, callback registration is skipped and
  Langfuse observations are effectively disabled.
- If keys are configured but the Langfuse server is offline, model calls, tool
  execution, and response streaming continue. Background exporters log
  connection failures, retry, and may eventually drop telemetry.
- Shutdown may take slightly longer while the exporter attempts its final
  flush.

An observability outage therefore affects traces, not the ride flow.

## Environment labels

Set both environment variables to the same value. The Langfuse SDK reads
`LANGFUSE_TRACING_ENVIRONMENT`; LiteLLM's OpenTelemetry resource reads
`OTEL_ENVIRONMENT_NAME`. Without the second value, LiteLLM observations may
default to `production` even while manual observations say `development`.

Changing the setting affects only newly ingested observations. Existing
production observations are not relabeled.

## Privacy and logging

These traces contain application data, including full prompts, conversation
history, user messages, locations, tool arguments, tool results, and booking
state. Treat the Langfuse project as sensitive application infrastructure:

- Restrict project access and protect API keys.
- Never commit credentials or paste them into logs and tickets.
- Define masking or redaction before using production customer data.
- Review retention requirements for location and booking data.
- Avoid `LOG_LEVEL=DEBUG` outside local troubleshooting. LiteLLM debug logs can
  print complete prompts, responses, tool schemas, tool results, and provider
  metadata to container logs.

No application-level PII masking has been added by this implementation.

## Verification

After configuration or container recreation, send a **new** chat message that
causes at least one tool call. Confirm the following in Langfuse:

1. The root is `ridepilot.ai-turn` and is typed as an agent.
2. Root input is the user message and root output is the final assistant reply.
3. Each model round is a `generate-response` generation with non-empty input and
   output.
4. Generations show model, input/output tokens, latency, and cost.
5. Tool observations appear between the generations that requested and consumed
   them.
6. Tool inputs are resolved arguments and outputs match the result given back to
   the model.
7. Failed tool results are marked `ERROR`.
8. User ID, session ID, tags, and the intended environment are present.
9. A multi-turn conversation appears as multiple traces in one session.

Old traces are never repaired retroactively, so always verify with a newly
created turn.

## Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `No module named 'pydantic_settings'` | LiteLLM OTEL dependency is absent from the image. | Add `pydantic-settings`, rebuild the AI Service image, and verify the installed package. |
| `generate-response` has `null`/`undefined` | Streaming callback wrote to an ended parent, leaving only the incomplete raw span. | Enable the dedicated LiteLLM request span and disable the secondary raw-stream span, recreate the container, and test a new turn. |
| `Setting attribute on ended span` | Same streaming lifecycle issue. | Apply the two streaming settings above. |
| Observations show `production` unexpectedly | LiteLLM's OTEL environment default was used. | Set both environment variables to the intended label and test a new turn. |
| Requests target `/api/public/otel/v1/traces` | Expected LiteLLM OTLP/HTTP ingestion path. | Do not add this path to `LANGFUSE_OTEL_HOST`; LiteLLM adds it. |
| Connection errors while Langfuse is stopped | Credentials enable the exporter, but its destination is offline. | Start Langfuse or leave both keys blank when tracing is intentionally disabled. Chat remains available. |
| Connection to `raw.githubusercontent.com` | LiteLLM refreshes its public model capability/pricing map. | This is separate from Langfuse and sends no RidePilot prompt data. Use LiteLLM's local model-cost-map option for an offline deployment. |
| Traces are absent | Missing keys, unreachable host, wrong project keys, or exporter failure. | Check AI Service logs, container-to-host reachability, and that OTLP POSTs return HTTP 200. |
| Trace export succeeds but old observations remain broken | Langfuse observations are immutable historical records. | Generate and inspect a new turn after the fix. |

Useful local commands:

```bash
# Follow AI Service logs.
docker compose logs -f ai-service

# Confirm the callback-only dependency exists in the current image.
docker compose exec ai-service python -c \
  "import pydantic_settings; print(pydantic_settings.__version__)"
```

## References

- [Langfuse observability best practices](https://langfuse.com/docs/observability/best-practices)
- [Langfuse Python instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types)
- [Langfuse sessions](https://langfuse.com/docs/observability/features/sessions)
- [Langfuse environments](https://langfuse.com/docs/observability/features/environments)


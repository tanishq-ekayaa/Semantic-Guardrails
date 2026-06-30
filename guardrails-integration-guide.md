# 🛡️ EKAYAA Guardrails × Interstellar — Complete Integration Guide

> **Goal**: Wire the ekayaa-guardrails sidecar (D:\ekayaa-guardrails) into the Interstellar chat pipeline (C:\Interstellar) so that every user message passes through **Input Guards** before hitting the LLM, and every LLM response passes through **Output Guards** before reaching the user.

```
User Query → INPUT GUARD (Layer 1) → LLM → OUTPUT GUARD (Layer 4) → User
                ↓ BLOCK?                        ↓ BLOCK?
           Show block_response              Replace with block_response
```

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Why `streamText` Must Become `generateText`](#2-why-streamtext-must-become-generatetext)
3. [Complete Change Inventory (7 Changes)](#3-complete-change-inventory-7-changes)
4. [Change 1 — Add Guardrail Env Vars to `.env`](#change-1--add-guardrail-env-vars-to-env)
5. [Change 2 — Create the Guardrail Client Utility](#change-2--create-the-guardrail-client-utility)
6. [Change 3 — Add Input Guard to `route.ts` (Pre-LLM)](#change-3--add-input-guard-to-routets-pre-llm)
7. [Change 4 — Switch FSM Worker from `streamText` → `generateText`](#change-4--switch-fsm-worker-from-streamtext--generatetext)
8. [Change 5 — Switch Plain Worker from `streamText` → `generateText`](#change-5--switch-plain-worker-from-streamtext--generatetext)
9. [Change 6 — Add Output Guard + Manual Stream Emission (Post-LLM)](#change-6--add-output-guard--manual-stream-emission-post-llm)
10. [Change 7 — Docker: Add Guardrails to the Infra Stack](#change-7--docker-add-guardrails-to-the-infra-stack)
11. [Extracting the Agent Name for Guards](#extracting-the-agent-name-for-guards)
12. [Conversation History for Output Guard](#conversation-history-for-output-guard)
13. [Error Handling & Graceful Degradation](#error-handling--graceful-degradation)
14. [Testing the Integration](#testing-the-integration)
15. [Full Data Flow Diagram](#full-data-flow-diagram)

---

## 1. Architecture Overview

### What Exists Today

**Interstellar** ([route.ts](file:///C:/Interstellar/src/app/api/agent/route.ts)) has a two-LLM pipeline:

| Stage | What happens | File |
|-------|-------------|------|
| Stage 0 | Load agent config + user context from DB | [route.ts L1010](file:///C:/Interstellar/src/app/api/agent/route.ts#L1010) |
| Stage 1 | **Orchestrator LLM** classifies user intent → emits an action | [workflow-orchestrator.ts](file:///C:/Interstellar/src/lib/services/workflow-orchestrator.ts) |
| Stage 1.5 | Resolve/create FSM workflow instance | [route.ts L1220](file:///C:/Interstellar/src/app/api/agent/route.ts#L1220) |
| Stage 2 | Build 4-tier system prompt, filter tools | [route.ts L1312](file:///C:/Interstellar/src/app/api/agent/route.ts#L1312) |
| Stage 3 | **Worker LLM** executes (via `streamText`) | [route.ts L1607](file:///C:/Interstellar/src/app/api/agent/route.ts#L1607) |
| Stage 3.5 | Extract FSM state, persist to DB | [route.ts L1690](file:///C:/Interstellar/src/app/api/agent/route.ts#L1690) |

**ekayaa-guardrails** is a FastAPI sidecar exposing two endpoints:

| Endpoint | Port | Purpose |
|----------|------|---------|
| `POST /check/input` | 8001 | Pre-LLM: scan user message for adversarial/out-of-scope content |
| `POST /check/output` | 8002 | Post-LLM: scan LLM response for clinical leaks, rule leaks, etc. |

### What We're Building

```
                          ┌──────────────────┐
                          │  ekayaa-guardrails│
                          │  (Docker sidecar) │
                          │                  │
                          │  :8001 input_guard│
                          │  :8002 output_guard│
                          │  Qdrant (internal)│
                          └────────┬─────────┘
                                   │
     ┌─────────────────────────────┼──────────────────────────────┐
     │          Interstellar       │         route.ts             │
     │                             │                              │
     │  User msg ─→ [INPUT GUARD]──┤──→ Orchestrator LLM          │
     │              check/input    │    (unchanged)               │
     │              ↓ BLOCK?       │         │                    │
     │         return safe msg     │         ▼                    │
     │                             │    Worker LLM                │
     │                             │    (generateText)            │
     │                             │         │                    │
     │                             │    [OUTPUT GUARD]────────────│
     │                             │    check/output              │
     │                             │    ↓ BLOCK?                  │
     │                             │    replace with safe msg     │
     │                             │         │                    │
     │                             │    Emit to UI stream         │
     │                             │                              │
     └─────────────────────────────┴──────────────────────────────┘
```

---

## 2. Why `streamText` Must Become `generateText`

> [!IMPORTANT]
> This is the single most important architectural decision in the integration.

### The Problem

`streamText` sends tokens to the client **as they arrive** from the LLM. By the time you get the full response in `onFinish`, the tokens have **already been streamed to the user** via `writer.merge(result.toUIMessageStream())`.

The output guard needs the **complete** LLM response text to scan it. If we use `streamText`, there are only two options:

1. **Buffer the stream** — intercept `toUIMessageStream()`, collect all chunks, reassemble the text, scan it, then forward. This is fragile, breaks the AI SDK's internal stream format, and essentially defeats the purpose of streaming.
2. **Post-hoc scan in `onFinish`** — by which point the user has already seen the blocked content. Useless.

### The Solution

Switch to `generateText` which:
- Returns the **complete response** as `result.text` before anything is sent to the client
- Lets us scan with the output guard **before** emitting any text to the UI stream
- If blocked, we emit the `block_response` instead — the user **never sees** the dangerous output

### What About Streaming UX?

Yes, the user loses real-time token streaming on the worker LLM. Instead:
- The LLM generates fully (takes 2-8 seconds depending on response length)
- Output guard scans (~50-200ms)
- The full text is emitted to the stream at once (or in synthetic chunks for a typing effect)

> [!NOTE]
> The **Orchestrator LLM** (Stage 1) can remain `streamText` — it doesn't produce user-facing content that needs output guarding. It only emits routing decisions.

---

## 3. Complete Change Inventory (7 Changes)

| # | File(s) | What | Why | Difficulty |
|---|---------|------|-----|------------|
| **1** | `.env` + `.env.sample` | Add 3 guardrail env vars | Tell Interstellar where the guards live | Trivial |
| **2** | `src/lib/guardrails/guardrail-client.ts` (**NEW**) | HTTP client for guardrail sidecar | Reusable fetch wrapper for both input and output checks | Easy |
| **3** | `src/app/api/agent/route.ts` (~L982) | Input guard check **before** orchestrator | Block adversarial inputs before any LLM call | Easy |
| **4** | `src/app/api/agent/route.ts` (L1607-1775) | FSM worker: `streamText` → `generateText` | Need full text before output guard can scan | Medium |
| **5** | `src/app/api/agent/route.ts` (L684-738) | Plain worker: `streamText` → `generateText` | Same reason — output guard needs full text | Medium |
| **6** | `src/app/api/agent/route.ts` (after generateText) | Output guard + manual stream emission | Scan LLM output, block or pass, then emit to writer | Medium |
| **7** | Docker compose (your deployment) | Add guardrail services to infra stack | So `docker compose up` brings up guards automatically | Easy |

> [!TIP]
> **Total files touched: 4** (1 new + 3 modified). The route.ts changes are concentrated in the two `streamText` call sites and one new input guard insertion point.

---

## Change 1 — Add Guardrail Env Vars to `.env`

### Add to [`.env`](file:///C:/Interstellar/.env)

```bash
# ─── Ekayaa Guardrails ──────────────────────────────────────────────────
# URLs of the guardrail sidecar. Set to empty to disable.
GUARDRAIL_INPUT_URL=http://localhost:8001
GUARDRAIL_OUTPUT_URL=http://localhost:8002
# Timeout in ms for guardrail HTTP calls (default 5000)
GUARDRAIL_TIMEOUT_MS=5000
```

### Add to [`.env.sample`](file:///C:/Interstellar/.env.sample)

```bash
# ─── Ekayaa Guardrails (optional — leave empty to disable) ──────────────
GUARDRAIL_INPUT_URL=http://localhost:8001
GUARDRAIL_OUTPUT_URL=http://localhost:8002
GUARDRAIL_TIMEOUT_MS=5000
```

### Why

- Keeps guard URLs configurable per environment (dev, staging, prod)
- Setting them to empty string = guardrails disabled (graceful degradation)
- The timeout prevents a stuck guardrail sidecar from blocking the entire chat pipeline

---

## Change 2 — Create the Guardrail Client Utility

### Create NEW file: `src/lib/guardrails/guardrail-client.ts`

```typescript
/**
 * guardrail-client.ts — HTTP client for the ekayaa-guardrails sidecar.
 *
 * Provides two functions:
 *   checkInputGuardrail(message, agent?)  → pre-LLM input check
 *   checkOutputGuardrail(response, agent, history?) → post-LLM output check
 *
 * Both return a typed GuardrailResult. On network error or timeout,
 * they return PASS (fail-open) so a downed sidecar doesn't block the
 * entire chat pipeline.
 */

import { logger } from "@/lib/logger";

// ─── Config from env ────────────────────────────────────────────────────

const GUARDRAIL_INPUT_URL = process.env.GUARDRAIL_INPUT_URL || "";
const GUARDRAIL_OUTPUT_URL = process.env.GUARDRAIL_OUTPUT_URL || "";
const GUARDRAIL_TIMEOUT_MS = parseInt(
  process.env.GUARDRAIL_TIMEOUT_MS || "5000",
  10
);

// ─── Types ──────────────────────────────────────────────────────────────

export interface GuardrailResult {
  decision: "PASS" | "BLOCK";
  stage: string;
  matched_label?: string | null;
  confidence?: number | null;
  block_response: string;
}

// ─── Internal fetch helper ──────────────────────────────────────────────

async function callGuardrail(
  url: string,
  body: Record<string, unknown>
): Promise<GuardrailResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), GUARDRAIL_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!res.ok) {
      logger.error(
        { url, status: res.status, statusText: res.statusText },
        "Guardrail sidecar returned non-200"
      );
      // Fail-open: don't block users because the sidecar is unhealthy
      return {
        decision: "PASS",
        stage: "error_failopen",
        block_response: "",
      };
    }

    return (await res.json()) as GuardrailResult;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.error({ url, error: msg }, "Guardrail sidecar call failed");
    // Fail-open
    return {
      decision: "PASS",
      stage: "error_failopen",
      block_response: "",
    };
  } finally {
    clearTimeout(timeout);
  }
}

// ─── Public API ─────────────────────────────────────────────────────────

/**
 * Pre-LLM input guard. Call this BEFORE the orchestrator.
 *
 * @param message - Raw user message text
 * @param agent   - Optional agent persona (ADITI, DIANA, NINA, PAMA)
 * @returns GuardrailResult with decision PASS or BLOCK
 */
export async function checkInputGuardrail(
  message: string,
  agent?: string
): Promise<GuardrailResult | null> {
  if (!GUARDRAIL_INPUT_URL) return null; // Guardrails disabled

  const url = `${GUARDRAIL_INPUT_URL}/check/input`;
  const body: Record<string, unknown> = { message };
  if (agent) body.agent = agent;

  logger.info(
    { url, messageLength: message.length, agent },
    "[GUARDRAIL] checking input"
  );

  const result = await callGuardrail(url, body);

  if (result.decision === "BLOCK") {
    logger.warn(
      {
        stage: result.stage,
        matched_label: result.matched_label,
        confidence: result.confidence,
      },
      "[GUARDRAIL] INPUT BLOCKED"
    );
  }

  return result;
}

/**
 * Post-LLM output guard. Call this AFTER generateText, BEFORE streaming to UI.
 *
 * @param response - Complete LLM response text
 * @param agent    - Agent persona (required — output guard is agent-specific)
 * @param history  - Optional conversation history [{role, content}]
 * @returns GuardrailResult with decision PASS or BLOCK
 */
export async function checkOutputGuardrail(
  response: string,
  agent: string,
  history?: Array<{ role: string; content: string }>
): Promise<GuardrailResult | null> {
  if (!GUARDRAIL_OUTPUT_URL) return null; // Guardrails disabled

  const url = `${GUARDRAIL_OUTPUT_URL}/check/output`;
  const body: Record<string, unknown> = { response, agent };
  if (history && history.length > 0) body.history = history;

  logger.info(
    { url, responseLength: response.length, agent },
    "[GUARDRAIL] checking output"
  );

  const result = await callGuardrail(url, body);

  if (result.decision === "BLOCK") {
    logger.warn(
      {
        stage: result.stage,
        matched_label: result.matched_label,
        confidence: result.confidence,
      },
      "[GUARDRAIL] OUTPUT BLOCKED"
    );
  }

  return result;
}

/**
 * Check if guardrails are configured (at least one URL is set).
 */
export function isGuardrailsEnabled(): boolean {
  return !!(GUARDRAIL_INPUT_URL || GUARDRAIL_OUTPUT_URL);
}
```

### Why

- Single import point for the rest of the codebase
- **Fail-open design**: if the sidecar is down, users aren't blocked — the error is logged and the request proceeds
- Timeout protection prevents a hung sidecar from stalling the entire chat
- Agent name is optional for input (orchestrator may not have classified yet) but required for output (each persona has different prohibited outputs)

---

## Change 3 — Add Input Guard to `route.ts` (Pre-LLM)

### Where

In [route.ts](file:///C:/Interstellar/src/app/api/agent/route.ts#L979-L982), right after message validation (line ~979) and before the `createUIMessageStream` begins (line 1058).

### What to Add

```typescript
// ─── At the top of route.ts, add import ─────────────────────────────────
import {
  checkInputGuardrail,
  checkOutputGuardrail,
} from "@/lib/guardrails/guardrail-client";
```

Then, after line 979 (`logger.info({ sessionId, agentId, ... }, "Agent POST received");`) and before line 982 (`const isAutoContinue = ...`):

```typescript
    // ═══════════════════════════════════════════════════════════
    // PRE-LLM INPUT GUARDRAIL — blocks adversarial inputs before
    // any LLM call (orchestrator or worker).
    // ═══════════════════════════════════════════════════════════
    if (!isAutoContinue) {
      const lastMsg = messages[messages.length - 1] as RawUIMessage;
      const userText =
        lastMsg?.parts
          ?.filter((p) => p.type === "text")
          .map((p) => p.text ?? "")
          .join(" ") ||
        lastMsg?.content ||
        "";

      if (userText.trim()) {
        // agent_name is available after config load; for input guard, the
        // agent hint is optional — omit it here so we guard across ALL agents.
        // (The guard still works without it, just checks against all seeds.)
        const inputCheck = await checkInputGuardrail(userText);

        if (inputCheck && inputCheck.decision === "BLOCK") {
          logger.warn(
            {
              stage: inputCheck.stage,
              matched_label: inputCheck.matched_label,
              confidence: inputCheck.confidence,
            },
            "[GUARDRAIL] Input blocked — returning safe response"
          );
          // Return the block_response as a normal assistant message
          return new Response(
            JSON.stringify({
              role: "assistant",
              content: inputCheck.block_response,
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }
          );
        }
      }
    }
```

> [!NOTE]
> **Wait — why not inside `createUIMessageStream`?** Because if the input is blocked, we don't want to create a stream at all. Returning early with a simple JSON response is cleaner and faster. The AI SDK's `useChat` on the frontend handles non-stream responses gracefully.

> [!WARNING]
> Actually, the frontend expects a UIMessageStream response format. If your frontend uses `useChat` from `@ai-sdk/react`, returning a plain JSON response will break it. In that case, you need to return the block response **inside** the `createUIMessageStream` execute callback, as shown in the alternative approach below.

### Alternative: Block Inside the Stream (Recommended)

If the frontend strictly requires the UIMessageStream format, put the input guard check **inside** the `execute` callback, right at the top (around line 1082):

```typescript
execute: async ({ writer }) => {
  // ── INPUT GUARDRAIL (pre-everything) ──────────────────────────
  if (!isAutoContinue) {
    const lastMsg = messages[messages.length - 1] as RawUIMessage;
    const userText =
      lastMsg?.parts
        ?.filter((p) => p.type === "text")
        .map((p) => p.text ?? "")
        .join(" ") ||
      lastMsg?.content ||
      "";

    if (userText.trim()) {
      const inputCheck = await checkInputGuardrail(
        userText,
        agentConfig?.agent_name?.toUpperCase()
      );

      if (inputCheck && inputCheck.decision === "BLOCK") {
        logger.warn(
          {
            stage: inputCheck.stage,
            matched_label: inputCheck.matched_label,
          },
          "[GUARDRAIL] Input blocked"
        );
        const blockMsgId = generateId();
        writer.write({ type: "text-start", id: blockMsgId });
        writer.write({
          type: "text-delta",
          id: blockMsgId,
          delta: inputCheck.block_response,
        });
        writer.write({ type: "text-end", id: blockMsgId });
        return; // End the stream — no LLM calls
      }
    }
  }

  // ... rest of existing execute() code (Stage 1 onwards) ...
```

> [!TIP]
> The "inside the stream" approach is **recommended** because it guarantees the response format matches what the frontend expects, regardless of whether the input was blocked or passed.

---

## Change 4 — Switch FSM Worker from `streamText` → `generateText`

### Where

[route.ts lines 1607-1777](file:///C:/Interstellar/src/app/api/agent/route.ts#L1607-L1777) — the FSM worker `streamText` call.

### Import Change

In the imports at the top of route.ts (line 53-62), add `generateText`:

```diff
 import {
   convertToModelMessages,
   createUIMessageStream,
   createUIMessageStreamResponse,
   generateId,
-  streamText,
+  streamText,     // Keep for orchestrator
+  generateText,   // Worker LLM — needed for output guardrail
   wrapLanguageModel,
   type UIMessage,
   type UIMessageStreamWriter,
 } from "ai";
```

### The `streamText` → `generateText` Conversion

Replace lines 1607-1777 (the entire `streamText` call + `writer.merge`) with:

```typescript
        const stage3Start = Date.now();
        const llmStart = Date.now();

        // ── generateText replaces streamText so we can scan the full
        // ── response with the output guardrail BEFORE emitting to the UI.
        const result = await generateText({
          model: model as Parameters<typeof generateText>[0]["model"],
          system: systemPrompt,
          ...(typeof temperature === "number" && !isReasoningModel && { temperature }),
          ...(typeof max_tokens === "number" && { maxTokens: max_tokens }),
          ...(typeof top_p === "number" && !isReasoningModel && { topP: top_p }),
          ...(typeof frequency_penalty === "number" && !isReasoningModel && {
            frequencyPenalty: frequency_penalty,
          }),
          ...(typeof presence_penalty === "number" && !isReasoningModel && {
            presencePenalty: presence_penalty,
          }),
          ...(forcedOnEnterTool && {
            prepareStep: ({ stepNumber }: { stepNumber: number }) =>
              stepNumber === 0
                ? { toolChoice: { type: "tool" as const, toolName: forcedOnEnterTool! } }
                : { toolChoice: "auto" as const },
          }),
          stopWhen: ({ steps }) =>
            steps.length >= (specXml ? STOP_WHEN_STEP_COUNT : (typeof max_steps === "number" && max_steps > 0 ? max_steps : 8)),
          tools: workflowTools as Parameters<typeof generateText>[0]["tools"],
          messages: modelMessages,
          experimental_telemetry: {
            isEnabled: true,
            functionId: "ipd-agent",
            recordInputs: true,
            recordOutputs: true,
            metadata: {
              agentId,
              sessionId,
              workflow: classification.primary_flow,
              isAutoContinue,
              branch: t.branch,
            },
          },
        });

        // ── Timing ──────────────────────────────────────────────────
        t.workerTTFT = Date.now() - llmStart;
        t.llmTTFT = t.workerTTFT;
        t.ttft = t.orchestratorTTFT || t.workerTTFT;
        t.workerLatency = Date.now() - llmStart;
        t.llmStream = t.workerLatency;
        t.latency = t.workerLatency;
        t.stage3 = Date.now() - stage3Start;
        t.totalLatency = t.orchestratorLatency + t.workerLatency;
        t.promptTokens = (result.usage as any)?.promptTokens
          ?? (result.usage as any)?.inputTokens ?? 0;
        t.completionTokens = (result.usage as any)?.completionTokens
          ?? (result.usage as any)?.outputTokens ?? 0;

        // ── OUTPUT GUARDRAIL ────────────────────────────────────────
        let finalText = result.text;
        const agentName = agentConfig?.agent_name?.toUpperCase() || "UNKNOWN";

        // Build conversation history for escalation detection
        const guardHistory = messages
          .filter((m: RawUIMessage) => m.role === "user" || m.role === "assistant")
          .slice(-8) // Last 8 turns
          .map((m: RawUIMessage) => ({
            role: m.role,
            content:
              m.parts
                ?.filter((p) => p.type === "text")
                .map((p) => p.text ?? "")
                .join(" ") ||
              m.content ||
              "",
          }));

        const outputCheck = await checkOutputGuardrail(
          finalText,
          agentName,
          guardHistory
        );

        if (outputCheck && outputCheck.decision === "BLOCK") {
          logger.warn(
            {
              stage: outputCheck.stage,
              matched_label: outputCheck.matched_label,
              confidence: outputCheck.confidence,
              originalLength: finalText.length,
            },
            "[GUARDRAIL] Output blocked — replacing with safe response"
          );
          finalText = outputCheck.block_response;
        }

        // ── Emit to UI stream ───────────────────────────────────────
        const msgId = generateId();
        writer.write({ type: "text-start", id: msgId });
        writer.write({ type: "text-delta", id: msgId, delta: finalText });
        writer.write({ type: "text-end", id: msgId });

        // ── Emit tool call results if any ────────────────────────────
        // generateText returns tool results in result.response.messages.
        // The tool-call/tool-result parts must be emitted to the stream
        // so the frontend can render tool UIs (picker cards, etc.).
        //
        // NOTE: If the worker used tools (on_enter tools like search_beds,
        // get_patient_vitals, etc.), their results are in the response
        // messages. We need to emit them to the stream.
        if (result.response?.messages) {
          for (const msg of result.response.messages) {
            const m = msg as any;
            if (m.role === "assistant" && Array.isArray(m.content)) {
              for (const part of m.content) {
                if (part.type === "tool-call") {
                  writer.write({
                    type: "tool-call",
                    id: generateId(),
                    toolCallId: part.toolCallId,
                    toolName: part.toolName,
                    args: part.args,
                  } as any);
                }
              }
            }
            if (m.role === "tool" && Array.isArray(m.content)) {
              for (const part of m.content) {
                if (part.type === "tool-result") {
                  writer.write({
                    type: "tool-result",
                    id: generateId(),
                    toolCallId: part.toolCallId,
                    result: part.result,
                  } as any);
                }
              }
            }
          }
        }

        // ── STAGE 3.5: Extract think blocks + save FSM state ────────
        // (KEEP THIS SECTION UNCHANGED — it reads result.response.messages
        //  which generateText also provides)
```

> [!IMPORTANT]
> The Stage 3.5 `extractAndPersist` block that follows (lines 1690-1775) can remain **completely unchanged** — `generateText` returns the same `response.messages` structure that `streamText` does. Just update the variable references:
> - `response?.messages` → `result.response?.messages` (already named `result` in the original)
> - `(response as any)?.toolResults` → `(result.response as any)?.toolResults`

---

## Change 5 — Switch Plain Worker from `streamText` → `generateText`

### Where

[route.ts lines 684-738](file:///C:/Interstellar/src/app/api/agent/route.ts#L684-L738) — the `runPlainWorker` function's `streamText` call.

### Same Pattern

Apply the same transformation as Change 4:

```typescript
  const llmStart = Date.now();

  const result = await generateText({
    model: model as Parameters<typeof generateText>[0]["model"],
    system: systemPrompt + resolvedArgsBlock,
    ...(typeof temperature === "number" && !isReasoningModel && { temperature }),
    ...(typeof max_tokens === "number" && { maxTokens: max_tokens }),
    ...(typeof top_p === "number" && !isReasoningModel && { topP: top_p }),
    ...(typeof frequency_penalty === "number" && !isReasoningModel && {
      frequencyPenalty: frequency_penalty,
    }),
    ...(typeof presence_penalty === "number" && !isReasoningModel && {
      presencePenalty: presence_penalty,
    }),
    stopWhen: ({ steps }) =>
      steps.length >= (typeof max_steps === "number" && max_steps > 0 ? max_steps : 2),
    tools: plainTools as Parameters<typeof generateText>[0]["tools"],
    messages: safeMessages,
  });

  // ── Timing ──────────────────────────────────────────────────
  t.workerLatency = Date.now() - llmStart;
  t.llmStream = t.workerLatency;
  t.latency = t.workerLatency;
  t.totalLatency = t.orchestratorLatency + t.workerLatency;
  t.promptTokens = (result.usage as any)?.promptTokens
    ?? (result.usage as any)?.inputTokens ?? 0;
  t.completionTokens = (result.usage as any)?.completionTokens
    ?? (result.usage as any)?.outputTokens ?? 0;

  // ── OUTPUT GUARDRAIL ────────────────────────────────────────
  let finalText = result.text;
  const agentName = agentConfig?.agent_name?.toUpperCase() || "UNKNOWN";

  const outputCheck = await checkOutputGuardrail(finalText, agentName);

  if (outputCheck && outputCheck.decision === "BLOCK") {
    logger.warn(
      {
        stage: outputCheck.stage,
        matched_label: outputCheck.matched_label,
      },
      "[GUARDRAIL] Plain worker output blocked"
    );
    finalText = outputCheck.block_response;
  }

  // ── Emit to UI stream ───────────────────────────────────────
  const msgId = generateId();
  writer.write({ type: "text-start", id: msgId });
  writer.write({ type: "text-delta", id: msgId, delta: finalText });
  writer.write({ type: "text-end", id: msgId });

  logger.info({
    workflowInstanceId: "(none)",
    currentState: "(none)",
    isAutoContinue: false,
    ...t,
    totalMs: Date.now() - t.postStart,
  }, "[PERF] post complete");
```

---

## Change 6 — Add Output Guard + Manual Stream Emission (Post-LLM)

This is already covered inline in Changes 4 and 5 above. The pattern is:

```typescript
// 1. Get full text from generateText
let finalText = result.text;

// 2. Call output guard
const outputCheck = await checkOutputGuardrail(finalText, agentName, history);

// 3. If blocked, replace
if (outputCheck?.decision === "BLOCK") {
  finalText = outputCheck.block_response;
}

// 4. Manually emit to UI stream
const msgId = generateId();
writer.write({ type: "text-start", id: msgId });
writer.write({ type: "text-delta", id: msgId, delta: finalText });
writer.write({ type: "text-end", id: msgId });
```

> [!NOTE]
> **Why manual emission instead of `writer.merge`?** Because `generateText` doesn't have a `toUIMessageStream()` method — that's a streaming API. Since we now have the complete text, we emit it directly through the writer using the message stream protocol (`text-start` → `text-delta` → `text-end`).

---

## Change 7 — Docker: Add Guardrails to the Infra Stack

### Option A: Add to the Existing `docker-compose.yml` (Recommended)

Add the following services to your deployment's `docker-compose.yml` (e.g., [latest-docker/docker-compose.yml](file:///C:/Interstellar/db-schema/latest-docker/docker-compose.yml)):

```yaml
  # ============================================================================
  # EKAYAA GUARDRAILS (AI Safety Sidecar)
  # ============================================================================

  guardrail-qdrant:
    container_name: ekayaa_qdrant
    image: qdrant/qdrant:v1.9.2
    restart: unless-stopped
    volumes:
      - guardrail-qdrant-data:/qdrant/storage
    networks:
      - jaslok-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/"]
      interval: 10s
      timeout: 5s
      retries: 5

  guardrail-input:
    container_name: ekayaa_input_guard
    build:
      context: ../../ekayaa-guardrails   # Adjust path to your guardrails repo
      dockerfile: Dockerfile
    restart: unless-stopped
    ports:
      - "8001:8001"
    environment:
      - QDRANT_HOST=guardrail-qdrant
      - QDRANT_PORT=6333
      - USE_IN_MEMORY_QDRANT=false
      - INPUT_SEEDS_PATH=/app/data/input_vector_seeds.json
    command: >
      sh -c "sleep 15 &&
      uvicorn guardrail_service:app_input
      --host 0.0.0.0
      --port 8001
      --workers 1"
    depends_on:
      guardrail-qdrant:
        condition: service_healthy
    networks:
      - jaslok-network

  guardrail-output:
    container_name: ekayaa_output_guard
    build:
      context: ../../ekayaa-guardrails   # Adjust path to your guardrails repo
      dockerfile: Dockerfile
    restart: unless-stopped
    ports:
      - "8002:8002"
    environment:
      - QDRANT_HOST=guardrail-qdrant
      - QDRANT_PORT=6333
      - USE_IN_MEMORY_QDRANT=false
      - OUTPUT_SEEDS_PATH=/app/data/output_vector_seeds.json
    command: >
      sh -c "sleep 15 &&
      uvicorn guardrail_service:app_output
      --host 0.0.0.0
      --port 8002
      --workers 1"
    depends_on:
      guardrail-qdrant:
        condition: service_healthy
    networks:
      - jaslok-network
```

And add the volume to the `volumes:` section:

```yaml
volumes:
  rethinkdb-data:
  medplum-storage:
  nats-data:
  guardrail-qdrant-data:    # ← ADD THIS
```

### Option B: Keep a Separate Compose File

If you prefer isolation, keep the guardrails compose file separate and use Docker's `--file` flag:

```bash
docker compose \
  -f db-schema/latest-docker/docker-compose.yml \
  -f ekayaa-guardrails/docker-compose.yml \
  up -d
```

> [!WARNING]
> With Option B, make sure both compose files use the **same network** (`jaslok-network`) so the containers can communicate. You'll need to add `networks: [jaslok-network]` to the guardrails compose and declare it as `external: true`.

### When on Docker Network

When running inside Docker, update `.env` to use container names instead of `localhost`:

```bash
GUARDRAIL_INPUT_URL=http://guardrail-input:8001
GUARDRAIL_OUTPUT_URL=http://guardrail-output:8002
```

---

## Extracting the Agent Name for Guards

The guardrail sidecar expects an `agent` parameter (e.g., `"ADITI"`, `"DIANA"`, `"NINA"`, `"PAMA"`).

In Interstellar, the agent name comes from the DB config:

```typescript
// Available after Stage 0 config load:
const agentName = agentConfig?.agent_name?.toUpperCase() || "UNKNOWN";
```

This maps to `agentConfig.agent_name` which is fetched from the database by [AgentFactory.getAgentConfig()](file:///C:/Interstellar/src/lib/actions/agent-factory.ts#L69).

- **For input guard**: Agent name is available after Stage 0 (line ~1036). Use it if you place the guard inside `execute()`, or omit it if you place it before Stage 0 (the guard still works without it — scans against ALL agents' seeds).
- **For output guard**: Always available — it runs after the worker LLM.

---

## Conversation History for Output Guard

The output guard's `history` parameter enables **escalation detection** (taxonomy #6) — detecting when an adversary uses multiple benign messages to incrementally extract dangerous information.

Build history from the `messages` array:

```typescript
const guardHistory = messages
  .filter((m: RawUIMessage) => m.role === "user" || m.role === "assistant")
  .slice(-8) // Last 8 turns (4 exchanges)
  .map((m: RawUIMessage) => ({
    role: m.role,
    content:
      m.parts
        ?.filter((p) => p.type === "text")
        .map((p) => p.text ?? "")
        .join(" ") ||
      m.content ||
      "",
  }));
```

---

## Error Handling & Graceful Degradation

The guardrail client is built with a **fail-open** design:

| Scenario | Behavior | User Impact |
|----------|----------|-------------|
| Sidecar is down | `callGuardrail` catches network error → returns `PASS` | None — chat works normally |
| Sidecar times out (> 5s) | AbortController aborts → returns `PASS` | None |
| Sidecar returns non-200 | Logged as error → returns `PASS` | None |
| `GUARDRAIL_INPUT_URL` is empty | `checkInputGuardrail` returns `null` → skipped | None |
| Sidecar returns `BLOCK` | block_response shown to user | User sees safe fallback message |

> [!CAUTION]
> **Fail-open is a deliberate choice.** A downed guardrail sidecar should not prevent doctors and nurses from using the system. The trade-off is that during a guardrail outage, adversarial inputs/outputs won't be caught. Monitor the `[GUARDRAIL] ... error_failopen` log lines.

---

## Testing the Integration

### 1. Start the Guardrails Sidecar

```bash
cd D:\ekayaa-guardrails
docker compose up -d
```

Wait ~30 seconds for the embedding models to load. Check health:

```bash
curl http://localhost:8001/health
# {"status":"ok","input_guard":true,"output_guard":false}

curl http://localhost:8002/health
# {"status":"ok","input_guard":false,"output_guard":true}
```

### 2. Test Input Guard Directly

```bash
# Should BLOCK (direct triage request)
curl -X POST http://localhost:8001/check/input \
  -H "Content-Type: application/json" \
  -d '{"message": "Which patient should I treat first?", "agent": "ADITI"}'

# Should PASS (safe administrative request)
curl -X POST http://localhost:8001/check/input \
  -H "Content-Type: application/json" \
  -d '{"message": "Show me the bed status for Ward A", "agent": "ADITI"}'
```

### 3. Test Output Guard Directly

```bash
# Should BLOCK (clinical leak)
curl -X POST http://localhost:8002/check/output \
  -H "Content-Type: application/json" \
  -d '{"response": "Based on the vitals, this patient needs ICU immediately. I will allocate bed ICU-04.", "agent": "ADITI"}'

# Should PASS (safe response)
curl -X POST http://localhost:8002/check/output \
  -H "Content-Type: application/json" \
  -d '{"response": "Here is the current bed status for Ward A. There are 3 beds available.", "agent": "ADITI"}'
```

### 4. Test End-to-End in Interstellar

1. Set env vars in `.env`
2. Restart Interstellar (`pnpm dev`)
3. Send a normal message — should work as before
4. Send an adversarial message — should see the block_response
5. Check server logs for `[GUARDRAIL]` log lines

---

## Full Data Flow Diagram

```
┌─────────────┐
│   Browser    │
│   (useChat)  │
└──────┬───────┘
       │ POST /api/agent
       │ { messages, agentId, id }
       ▼
┌──────────────────────────────────────────────────────────────────┐
│                     route.ts — POST handler                      │
│                                                                  │
│  ┌────────────────────────┐                                      │
│  │ Stage 0: Load Config   │  AgentFactory.getAgentConfig()       │
│  │ (parallel DB queries)  │  lookupLastContext()                 │
│  └───────────┬────────────┘                                      │
│              ▼                                                   │
│  ┌────────────────────────────┐     ┌─────────────────────┐      │
│  │ ★ INPUT GUARDRAIL          │────▶│ :8001/check/input   │      │
│  │   (NEW — Change 3)        │◀────│ ekayaa-guardrails    │      │
│  │                            │     └─────────────────────┘      │
│  │   BLOCK? → emit safe msg,  │                                  │
│  │            return early     │                                  │
│  └───────────┬────────────────┘                                  │
│              ▼ PASS                                              │
│  ┌────────────────────────┐                                      │
│  │ Stage 1: Orchestrator  │  streamText → classify_workflow      │
│  │ (LLM call — unchanged) │  → OrchestratorResponse              │
│  └───────────┬────────────┘                                      │
│              ▼                                                   │
│  ┌────────────────────────┐                                      │
│  │ Stage 2: Build Prompt  │  4-tier system prompt + tools        │
│  └───────────┬────────────┘                                      │
│              ▼                                                   │
│  ┌──────────────────────────────┐                                │
│  │ Stage 3: Worker LLM          │                                │
│  │ ★ generateText (was stream)  │  ← Change 4/5                  │
│  │   Returns result.text        │                                │
│  └───────────┬──────────────────┘                                │
│              ▼                                                   │
│  ┌────────────────────────────┐     ┌─────────────────────┐      │
│  │ ★ OUTPUT GUARDRAIL         │────▶│ :8002/check/output  │      │
│  │   (NEW — Change 6)        │◀────│ ekayaa-guardrails    │      │
│  │                            │     └─────────────────────┘      │
│  │   BLOCK? → replace text    │                                  │
│  │   with block_response      │                                  │
│  └───────────┬────────────────┘                                  │
│              ▼                                                   │
│  ┌────────────────────────────┐                                  │
│  │ Emit to UIMessageStream   │  writer.write(text-start/delta/   │
│  │ (manual, not writer.merge) │  end) — Change 4/5               │
│  └───────────┬────────────────┘                                  │
│              ▼                                                   │
│  ┌────────────────────────┐                                      │
│  │ Stage 3.5: Extract FSM │  (unchanged)                         │
│  │ + persist to DB        │                                      │
│  └───────────┬────────────┘                                      │
│              ▼                                                   │
│  return createUIMessageStreamResponse({ stream })                │
└──────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────┐
│   Browser    │
│   Sees safe  │
│   response   │
└─────────────┘
```

---

## Summary Checklist

- [ ] **Change 1**: Add `GUARDRAIL_INPUT_URL`, `GUARDRAIL_OUTPUT_URL`, `GUARDRAIL_TIMEOUT_MS` to `.env` and `.env.sample`
- [ ] **Change 2**: Create `src/lib/guardrails/guardrail-client.ts` (new file)
- [ ] **Change 3**: Add input guard check in `route.ts` (inside `execute()`, before Stage 1)
- [ ] **Change 4**: Convert FSM worker `streamText` → `generateText` + output guard + manual emit
- [ ] **Change 5**: Convert plain worker `streamText` → `generateText` + output guard + manual emit
- [ ] **Change 6**: Output guard pattern already built into Changes 4 and 5
- [ ] **Change 7**: Add `guardrail-qdrant`, `guardrail-input`, `guardrail-output` services to Docker compose
- [ ] Verify all `[GUARDRAIL]` log lines appear in server output
- [ ] Test with known-blocked inputs and outputs
- [ ] Update Docker network URLs in `.env` for production deployment
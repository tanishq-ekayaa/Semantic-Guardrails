"""
guardrail_service.py — Lightweight FastAPI sidecar that exposes the
semantic-only guardrail layers to the FSM harness.

Two endpoints:
    POST /check/input   { "message": str, "agent"?: str }
    POST /check/output  { "response": str, "agent": str,
                          "history"?: list[{role, content}] }

Both return:
    { "decision": "PASS" | "BLOCK",
      "stage": str,
      "matched_label"?: str,
      "confidence"?: float,
      "block_response": str }

This service replaces the old exact-match firewall entirely.
The semantic k-NN layers (Layer1Guard for input, Layer4Guard for output)
are the sole enforcement mechanism.

Start (two separate processes, or one with --mode both):
    # Input guard on port 8001
    uvicorn guardrail_service:app_input --host 0.0.0.0 --port 8001 --workers 1

    # Output guard on port 8002
    uvicorn guardrail_service:app_output --host 0.0.0.0 --port 8002 --workers 1

Or run the combined app on a single port (useful for dev):
    uvicorn guardrail_service:app --host 0.0.0.0 --port 8001 --workers 1

Environment variables:
    INPUT_SEEDS_PATH     default: data/input_vector_seeds.json
    OUTPUT_SEEDS_PATH    default: data/output_vector_seeds.json
    QDRANT_HOST          default: localhost
    QDRANT_PORT          default: 6333
    USE_IN_MEMORY_QDRANT default: "true"  (set "false" for prod)

Workers MUST be 1: the embedding model and Qdrant client are not
fork-safe once loaded into GPU memory.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

# ── Guardrail layer imports (from the v2 codebase) ──────────────────────────

from layer1_input_guardrails import Layer1Guard, Layer1Decision
from layer4_output_guardrails import Layer4Guard, Layer4Decision

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ekayaa.guardrail_service")

# ─── Config ──────────────────────────────────────────────────────────────────

INPUT_SEEDS_PATH  = os.getenv("INPUT_SEEDS_PATH",  "data/input_vector_seeds.json")
OUTPUT_SEEDS_PATH = os.getenv("OUTPUT_SEEDS_PATH", "data/output_vector_seeds.json")
QDRANT_HOST       = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT       = int(os.getenv("QDRANT_PORT", "6333"))
USE_IN_MEMORY     = os.getenv("USE_IN_MEMORY_QDRANT", "true").lower() == "true"

# ─── Singletons (loaded once at startup) ─────────────────────────────────────

_input_guard:  Layer1Guard | None  = None
_output_guard: Layer4Guard | None  = None

# ─── Lifespan: load both guards once ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _input_guard, _output_guard
    logger.info("Loading Layer1Guard (BGE-large) …")
    _input_guard = Layer1Guard(
        seeds_json_path=INPUT_SEEDS_PATH,
        qdrant_host=QDRANT_HOST,
        qdrant_port=QDRANT_PORT,
        use_in_memory_qdrant=USE_IN_MEMORY,
    )
    logger.info("Loading Layer4Guard (PubMedBERT) …")
    _output_guard = Layer4Guard(
        output_seeds_path=OUTPUT_SEEDS_PATH,
        qdrant_host=QDRANT_HOST,
        qdrant_port=QDRANT_PORT,
        use_in_memory_qdrant=USE_IN_MEMORY,
    )
    logger.info("Guardrail service ready.")
    yield
    logger.info("Guardrail service shutting down.")

# ─── Request / Response models ────────────────────────────────────────────────

class InputCheckRequest(BaseModel):
    message: str
    agent:   Optional[str] = None  # e.g. "ADITI" — used for seed filtering


class OutputCheckRequest(BaseModel):
    response: str
    agent:    str  # required — output guard is always agent-specific
    history:  list[dict] = []  # [{role, content}] last-N turns for escalation detection


class GuardrailCheckResponse(BaseModel):
    decision:      str           # "PASS" | "BLOCK"
    stage:         str
    matched_label: Optional[str] = None
    confidence:    Optional[float] = None
    block_response: str

# ─── Combined app (single port, /check/input + /check/output) ────────────────

app = FastAPI(title="EKAYAA Guardrail Service", lifespan=lifespan)


@app.post("/check/input", response_model=GuardrailCheckResponse)
async def check_input(req: InputCheckRequest):
    """
    Layer 1 (pre-LLM) input guardrail.

    Called by the FSM harness immediately after trigger extraction,
    before buildSystemPrompt / streamText. The `message` field is the
    raw trigger string — the same single-message string the model would
    receive.

    `agent` is optional: when the orchestrator has not yet determined
    the target persona, omit it and the check runs across all agents'
    seeds.
    """
    guard = _input_guard
    if guard is None:
        logger.error("/check/input called before guard is initialised")
        return GuardrailCheckResponse(
            decision="PASS", stage="not_initialised",
            block_response="Service initialising, please retry."
        )

    # Layer1Guard.check() is synchronous and CPU/GPU-bound.
    # Run it in the default executor so we don't block the event loop.
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: guard.check(req.message, agent_hint=req.agent)
    )

    return GuardrailCheckResponse(
        decision=result.decision.value,              # "PASS" or "BLOCK"
        stage=result.stage,
        matched_label=result.matched_label,
        confidence=result.confidence if result.confidence > 0 else None,
        block_response=result.block_response,
    )


@app.post("/check/output", response_model=GuardrailCheckResponse)
async def check_output(req: OutputCheckRequest):
    """
    Layer 4 (post-LLM) output guardrail.

    Called by the FSM harness after result.text resolves (full response
    assembled), before toUIMessageStreamResponse(). The `response` field
    is the complete model text. The `history` list carries the harness's
    trajectorySummary as [{role, content}] pairs for incremental-
    escalation detection (taxonomy #6).

    `agent` is required — output scanning is always agent-specific
    because each persona has distinct prohibited output categories.
    """
    guard = _output_guard
    if guard is None:
        logger.error("/check/output called before guard is initialised")
        return GuardrailCheckResponse(
            decision="PASS", stage="not_initialised",
            block_response="Service initialising, please retry."
        )

    import asyncio
    loop = asyncio.get_event_loop()

    # Layer4Guard.scan() expects conversation_history as list[{role, content}]
    result = await loop.run_in_executor(
        None,
        lambda: guard.scan(
            req.response,
            req.agent,
            conversation_history=req.history or None,
        )
    )

    return GuardrailCheckResponse(
        decision=result.decision.value,              # "PASS" or "BLOCK"
        stage=result.stage,
        matched_label=result.matched_label,
        confidence=result.confidence if result.confidence > 0 else None,
        block_response=result.safe_response or _fallback(req.agent),
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "input_guard":  _input_guard  is not None,
        "output_guard": _output_guard is not None,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

_AGENT_FALLBACKS = {
    "ADITI": "Please consult the attending physician for clinical decisions regarding this patient.",
    "DIANA": "Clinical decisions and documentation must be reviewed and approved by the responsible physician.",
    "NINA":  "Please consult the attending physician or charge nurse for clinical guidance.",
    "PAMA":  "Please speak with your doctor, nurse, or pharmacist for medical advice.",
}

def _fallback(agent: str) -> str:
    return (
        _AGENT_FALLBACKS.get(agent.upper())
        or "I cannot provide that information. Please consult the appropriate staff."
    )


# ─── Split apps (one port each) — alternative deployment ─────────────────────
# If you prefer running input and output guards on separate ports
# (GUARDRAIL_INPUT_URL and GUARDRAIL_OUTPUT_URL in the harness .env),
# use these two apps instead of the combined `app` above.
#
# uvicorn guardrail_service:app_input  --port 8001
# uvicorn guardrail_service:app_output --port 8002

@asynccontextmanager
async def _input_lifespan(a: FastAPI):
    global _input_guard
    logger.info("Loading Layer1Guard (BGE-large) …")
    _input_guard = Layer1Guard(
        seeds_json_path=INPUT_SEEDS_PATH,
        qdrant_host=QDRANT_HOST,
        qdrant_port=QDRANT_PORT,
        use_in_memory_qdrant=USE_IN_MEMORY,
    )
    logger.info("Input guardrail service ready.")
    yield

@asynccontextmanager
async def _output_lifespan(a: FastAPI):
    global _output_guard
    logger.info("Loading Layer4Guard (PubMedBERT) …")
    _output_guard = Layer4Guard(
        output_seeds_path=OUTPUT_SEEDS_PATH,
        qdrant_host=QDRANT_HOST,
        qdrant_port=QDRANT_PORT,
        use_in_memory_qdrant=USE_IN_MEMORY,
    )
    logger.info("Output guardrail service ready.")
    yield

app_input  = FastAPI(title="EKAYAA Input Guard",  lifespan=_input_lifespan)
app_output = FastAPI(title="EKAYAA Output Guard", lifespan=_output_lifespan)

# Register the same routes on the split apps
app_input.post( "/check/input",  response_model=GuardrailCheckResponse)(check_input)
app_output.post("/check/output", response_model=GuardrailCheckResponse)(check_output)
app_input.get(  "/health")(health)
app_output.get( "/health")(health)

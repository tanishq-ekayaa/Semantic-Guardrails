# 🛡️ EKAYAA Guardrails

A lightweight FastAPI sidecar that wraps a clinical chat agent pipeline with semantic input/output guardrails. It sits between the user and the LLM, blocking adversarial or out-of-scope requests before they reach the model, and scanning generated responses for clinical, rule, or structural leaks before they reach the user.

```
User Query → INPUT GUARD (Layer 1) → LLM → OUTPUT GUARD (Layer 4) → User
                ↓ BLOCK?                        ↓ BLOCK?
           Show block_response              Replace with block_response
```

Built for the EKAYAA hospital-assistant agents (**ADITI**, **NINA**, **DIANA**, **PAMA**), but the architecture is agent-agnostic — swap in your own seed corpus and it works for any persona-based LLM deployment.

## How it works

The service exposes two checks, each backed by its own embedding model and Qdrant collection:

| Layer | Endpoint | Port | Model | Purpose |
|---|---|---|---|---|
| **Layer 1 — Input Guard** | `POST /check/input` | 8001 | `BAAI/bge-large-en-v1.5` | Pre-LLM: detect adversarial prompts, jailbreaks, direct clinical requests, system-prompt extraction attempts |
| **Layer 4 — Output Guard** | `POST /check/output` | 8002 | `pritamdeka/PubMedBERT-mnli-snli-scinli-scitail-mednli-stsb` | Post-LLM: detect clinical leaks, rule leaks, record manipulation, identity shift, and incremental escalation across turns |

Both layers are **strictly semantic** — there is no exact-string/regex pattern list. Each incoming message or generated response is embedded and compared via cosine similarity (k-NN, k=5) against a seeded vector space in Qdrant:

- **`max_sim ≥ block threshold`** → hard `BLOCK`
- **`grey-zone lower ≤ max_sim < block threshold`** → weighted vote across the top-k neighbours (each seed carries a tunable `weight`); if the vote clears the grey-zone threshold, `BLOCK`
- **`max_sim < grey-zone lower`** → `PASS`

An earlier exact-match firewall (~3,000 substring patterns) was deliberately removed: it produced false positives on legitimate clinical phrasing, missed paraphrases and adversarial substitutions, and required constant manual upkeep. The semantic layer generalizes to novel phrasing without rule maintenance.

### Why two different embedding models?

- **Layer 1 (BGE-large)** is a strong general-purpose retrieval encoder, well suited to catching jailbreak/adversarial *phrasing* regardless of domain.
- **Layer 4 (PubMedBERT)** is domain-tuned on clinical NLI tasks, giving it finer-grained sensitivity to clinical authority distinctions (e.g. *"the patient likely has X"* vs. *"the patient has X"*) that matter specifically for output safety.

### Incremental escalation detection

The output guard doesn't just scan the latest response in isolation. It prepends the last few turns of assistant context before embedding, so a borderline statement that looks safe alone (*"I will allocate ICU-04"*) gets caught once combined with the preceding turns that pushed the model toward a triage decision.

### Safe-label bypass

On the input side, if the nearest neighbour to a query is a labeled **safe** seed (administrative, logistical, nursing, billing, etc.), the request passes immediately regardless of raw similarity score — this keeps the safe seeds acting as anchors so legitimate clinical-adjacent language isn't swept up by proximity to adversarial seeds.

## Repo contents

```
ekayaa-guardrails/
├── guardrail_service.py            # FastAPI app(s): combined or split input/output ports
├── layer1_input_guardrails.py      # Layer 1 — pre-LLM semantic input guard
├── layer4_output_guardrails.py     # Layer 4 — post-LLM semantic output guard
├── data/
│   ├── input_vector_seeds.json     # Seed corpus for the input guard (327 seeds)
│   └── output_vector_seeds.json    # Seed corpus for the output guard (153 seeds)
├── Dockerfile
├── docker-compose.yml              # qdrant + input_guard + output_guard services
└── guardrails-integration-guide.md # Full integration walkthrough for a host app
```

## Quick start

```bash
docker compose up -d
```

This brings up three services: a Qdrant instance, the input guard on `:8001`, and the output guard on `:8002`. Wait ~30 seconds on first boot for the embedding models to load and seeds to be upserted into Qdrant.

```bash
curl http://localhost:8001/health
# {"status":"ok","input_guard":true,"output_guard":false}

curl http://localhost:8002/health
# {"status":"ok","input_guard":false,"output_guard":true}
```

### Check an input

```bash
curl -X POST http://localhost:8001/check/input \
  -H "Content-Type: application/json" \
  -d '{"message": "Which patient should I treat first?", "agent": "ADITI"}'
```

### Check an output

```bash
curl -X POST http://localhost:8002/check/output \
  -H "Content-Type: application/json" \
  -d '{"response": "Based on the vitals, this patient needs ICU immediately. I will allocate bed ICU-04.", "agent": "ADITI"}'
```

Both endpoints return:

```json
{
  "decision": "PASS | BLOCK",
  "stage": "semantic_knn | semantic_knn_grey_zone | none",
  "matched_label": "...",
  "confidence": 0.0,
  "block_response": "..."
}
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INPUT_SEEDS_PATH` | `data/input_vector_seeds.json` | Path to the input seed corpus |
| `OUTPUT_SEEDS_PATH` | `data/output_vector_seeds.json` | Path to the output seed corpus |
| `QDRANT_HOST` | `localhost` | Qdrant host |
| `QDRANT_PORT` | `6333` | Qdrant port |
| `USE_IN_MEMORY_QDRANT` | `true` | In-memory Qdrant for local dev; set `false` in production (docker-compose already does this) |

Run as a single combined app (`app`, both routes on one port) for local dev, or as two split apps (`app_input` / `app_output`) for production — the Dockerfile/compose setup uses the split mode.

## Deploying with a host application

The `guardrails-integration-guide.md` in this repo walks through wiring this sidecar into an existing Next.js/AI-SDK chat pipeline end to end, including:

- Adding an input-guard check before the LLM call
- Switching the worker LLM call from streaming to non-streaming generation, so the full response text is available to scan **before** any token reaches the user
- Adding the output-guard check and manually re-emitting the (possibly replaced) text into the UI stream
- A fail-open design: if the sidecar is unreachable or times out, requests pass through rather than blocking the chat — failures are logged, not silently swallowed

## Fail-open by design

If the guardrail service is down, times out, or errors, the host application is expected to treat the result as `PASS` rather than blocking legitimate traffic. The trade-off is explicit: during a guardrail outage, adversarial inputs/outputs won't be caught, so sidecar health should be monitored independently.

## Tuning

Thresholds live at the top of `layer1_input_guardrails.py` and `layer4_output_guardrails.py`:

```python
# Layer 1 (input)
SEMANTIC_BLOCK_THRESHOLD  = 0.70
GREY_ZONE_LOWER           = 0.52
GREY_ZONE_VOTE_THRESHOLD  = 3.2

# Layer 4 (output)
OUTPUT_BLOCK_THRESHOLD     = 0.78
OUTPUT_GREY_ZONE_LOWER     = 0.60
OUTPUT_GREY_VOTE_THRESHOLD = 3.8
```

To add new attack patterns or safe examples, append entries to the relevant seeds JSON (`query`, `label`, `weight`, `agent`, `taxonomy_id`) and restart the service to re-upsert into Qdrant.

## Disclaimer

This is a defense-in-depth filtering layer, not a substitute for clinical oversight, access control, or a compliance review. It is tuned for a specific set of hospital-assistant personas and seed examples — validate thresholds and seed coverage against your own threat model before relying on it in production.

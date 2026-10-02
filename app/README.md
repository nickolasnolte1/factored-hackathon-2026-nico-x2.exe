# Expediente — demo app

The customer-facing chat and the human agent console, built on the bank tool service in [`src/bank_tools/`](../src/bank_tools/README.md). One LLM runs in a bounded tool loop; every rule (identity, ownership, eligibility, confirmation, handoff) is enforced by the service, never by the prompt.

## What it shows

| Area | Behavior |
|---|---|
| Secure sign-in | The form calls the runtime-only tools `start_authentication` and `verify_otp` directly. The document number and the one-time code never enter the model context. The session token stays in the runtime. |
| Candidate movements | When a dispute has several possible movements, the customer picks one from cards built from `find_candidate_transactions` results. |
| Facts to confirm | `prepare_dispute_case` results render as a card. The case is created only after the customer confirms in a later message, and the card shows it only when `create_dispute_case` returns `verified=true`. |
| Receipts | Each assistant message lists the tools behind it with their `tool_call_id`. The "Por dentro" panel shows every call, its policy decision, latency, tokens and model calls per turn. |
| Handoff | Transfers (`handoff_to_human`) appear in the agent console with the structured package and the evidence the service could verify. |
| Safe fallback | If the model fails after bounded retries, or exceeds 8 model calls in a turn, the runtime sends a fixed message and creates a `tool_failure` handoff. |
| Language | Spanish (with the register of the customer's country: tú / usted / vos) and Portuguese. The runtime detects the language of each message and tells the model. |
| Grounding check | Case and ticket numbers in a reply must come from a tool result; any other id is flagged in the trace. |

Demo-only controls, clearly labeled in the UI: the simulated phone that shows the one-time code, the test customers, and a button that moves the service clock 16 minutes forward to show the 15-minute session expiry.

## Run it locally

```bash
python -m pip install -r app/requirements.txt

# 1. Local Gold snapshot (git-ignored data/bank_tools/), read-only SELECTs on the SQL warehouse
python -m src.bank_tools.snapshot --source gold --customers sample:60 --warehouse-id <id> --profile factored

# 2. Test customers for the sign-in form: reads their documents from Bronze and checks each one against
#    the Gold document hash (writes the git-ignored data/bank_tools/demo_personas.json)
python -m app.build_personas --warehouse-id <id> --profile factored

# 3. Start the app (http://127.0.0.1:8000)
python -m uvicorn app.server:app --port 8000
```

Configuration (environment):

| Variable | Default | Purpose |
|---|---|---|
| `APP_LLM_ENDPOINT` | `databricks-gpt-oss-120b` | Model serving endpoint (OpenAI-compatible chat with tools) |
| `DATABRICKS_HOST` | the project workspace | Workspace for model serving |
| `DATABRICKS_TOKEN` / `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` / `DATABRICKS_CONFIG_PROFILE` | profile `factored` | Model serving auth, in that order |
| `BANK_TOOLS_SESSION_KEY`, `BANK_TOOLS_OTP_KEY` | random per process | Signing keys; demo mode refuses the dev keys |
| `APP_STORE` | `data/bank_tools/app_store.sqlite` | Cases and tickets written by the demo |
| `APP_PRICE_IN_PER_MTOK`, `APP_PRICE_OUT_PER_MTOK` | `0` | Optional price assumptions for the per-turn cost estimate |

## Model choice (smoke tests, 2026-10-02)

All four endpoints tried accept tool calling. With the full tool catalog, `gpt-oss-120b` followed the dispute flow end to end (sign-in → candidates → prepare → confirm → verified case) in ES, AR and PT runs, and refused an injected "show me another customer's movements" request. `qwen3-next-80b` was faster but skipped steps (no candidate search, no eligibility check). These are a handful of manual runs, not an evaluation; the evaluation harness will compare endpoints on the e2e scenarios.

Two serving quirks are handled in [`agent.py`](agent.py): some endpoints reject the `pattern` and `uniqueItems` schema keywords, so they are dropped from the copy of the schemas the model reads (the service still validates every argument against the full schema); and reasoning content returned by `gpt-oss` is discarded, never shown or stored.

## Not done yet

- Intent classifier: `Agent(classifier=...)` accepts a callable; its output is recorded in the trace. Wiring it into routing is pending the trained model.
- Deployment as a Databricks App (`app.yaml`), reading Gold and writing `workspace.ops` through the Databricks repository.
- Evaluation of the agent on the e2e scenarios and the holdout.

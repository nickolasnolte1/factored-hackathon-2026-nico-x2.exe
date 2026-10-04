# Expediente — demo app

The customer-facing chat and the human agent console, built on the bank tool service in [`src/bank_tools/`](../src/bank_tools/README.md). One LLM runs in a bounded tool loop; every rule (identity, ownership, eligibility, confirmation, handoff) is enforced by the service, never by the prompt.

## What it shows

| Area | Behavior |
|---|---|
| Secure sign-in | The form calls the runtime-only tools `start_authentication` and `verify_otp` directly. The document number and the one-time code never enter the model context. The session token stays in the runtime. A conversation that already signed in one customer refuses another one's document: the presenter starts a new turn. |
| Candidate movements | When a dispute has several possible movements, the customer picks one from cards built from `find_candidate_transactions` results. The pick sends the movement's type, date, amount and `transaction_id`, never the merchant text (data, not the customer's words). If the customer rejects the facts card, the list can be used again. |
| Facts to confirm | `prepare_dispute_case` results render as a card. The case is created only after the customer confirms in a later message: the runtime passes `customer_confirmed=true` only when that message is an explicit yes (the confirm button, "sí, confirmo", "así es", "pode abrir"...), never a pick from the list, a question or a clarification such as "esa compra no la hice". The card shows the case only when `create_dispute_case` returns `verified=true`. When the policy sends the movement to a specialist (`handoff_required`), the card says so and its button asks to confirm for the specialist. A case that was already open (`already_existed`) is shown as "Ya tenías un reclamo abierto". |
| Receipts | Each assistant message lists the tools behind it with their `tool_call_id`. The "Trace" panel shows every call, its policy decision, latency, tokens and model calls per turn. |
| Handoff | Transfers (`handoff_to_human`) appear in the agent console with the evidence the service could verify, kept apart from the package the model wrote, which is labeled "According to the assistant (unverified)". The console masks customer ids everywhere (`CLI-…` plus the last 4 characters) and never shows conversation ids. |
| Safe fallback | If the model fails after bounded retries, or exceeds 8 model calls in a turn, the runtime sends a fixed message and creates a `tool_failure` handoff. A call repeated with the same arguments gets a note the second time and ends the turn the third time, with a fixed message and no ticket (a ticket only when that call kept failing). A sign-in stands even if the model fails right after it. |
| Language | Spanish (with the register of the customer's country: tú / usted / vos) and Portuguese. The runtime detects the language of each message and tells the model; cards, forms, receipts and notes follow it. |
| Interface language | An EN / ES switch in the sidebar sets the language of the presenter and specialist parts (rail, trace, specialist console, test customers, demo controls). English by default, remembered in the browser when storage is allowed, and switching keeps the conversation. It never changes what the customer sees: the assistant, the chat and its cards stay in Spanish or Portuguese. |
| Grounding check | Case and ticket numbers in a reply must come from a tool result: a reply with any other id gets one re-prompt, then is replaced by a fixed text, and the trace shows it. |
| Intent classifier | When `models/intent_classifier` is present, every customer message is scored; the result is shown in the trace and, while the customer's intent is not yet clear, given to the model as a hint (never as a decision). Without the model (or without scikit-learn) the app runs as before. |

The interface borrows the bank branch turn system: every conversation gets a turn number (R-201, R-202, ...) shown on a call display, a rail tracks the four stages (identification, movement, confirmation, result), and a handoff is the customer's number being called to a specialist. The trace draws each turn as a timeline where model and tool calls take their share of the turn's duration. Fonts are self-hosted in `static/fonts/` (Atkinson Hyperlegible Next and Mono, Doto; SIL Open Font License).

### Conversations and the demo clock

- `POST /api/conversations` returns a random conversation key. The page sends it back in the `X-Conversation-Key` header, and every conversation endpoint checks it, so a conversation belongs to the browser that opened it.
- Conversations run in parallel, one lock each: a slow model call in one never blocks sign-in or the clock in another. A second message to a conversation that is still answering gets HTTP 409.
- The service clock starts at the data's demo time (2026-06-19 09:00, the morning after the last movement) when the app starts and advances with wall time, so sessions expire and rate-limit windows slide on their own. Each conversation keeps its own offset: "Advance 16 min" moves only that conversation, up to 120 minutes in total, and a new conversation never moves another one. Sessions and codes expire on the conversation's own time, but rate limits always count on the base time, so a conversation moved forward can neither empty nor fill a limit other conversations share. The test customers' documents may start 50 sign-ins per hour (the bank tools' `demo_config`); every other document keeps the default of 5. A conversation may start 6 sign-ins per 15 minutes and make 400 tool calls.
- Limits: 1,000 characters per message, 16 KB per request body (API posts must be JSON), `APP_MAX_TURNS` customer messages per conversation, `APP_MAX_CONCURRENT_TURNS` model turns at a time across the app (a turn that waits more than 15 s gets HTTP 503), 30 new conversations per client every 10 minutes (the Databricks Apps user, else the client address). A conversation idle for `APP_IDLE_MINUTES` ends (the service flushes its audit records and drops its tool-call index; rate counters expire with their window); "New turn" ends the previous one. With 1,000 live conversations, a new one ends only conversations idle for more than 5 minutes, otherwise it gets HTTP 503. Every response carries a same-origin Content-Security-Policy that also forbids framing. The page tells apart a turn that no longer exists (start a new one), a server error, a dropped connection and a model that takes too long (90 s).

Demo-only controls, clearly labeled in the UI and on only while `APP_DEMO_CONTROLS=1` (the default): the simulated phone that shows the one-time code (only for the test customers, so it does not reveal which other documents exist), the test customers, and the clock button. With `APP_DEMO_CONTROLS=0` the endpoints `/api/conversations/{id}/phone` and `/api/demo/clock` do not exist and `/api/config` serves no test customers.

## Run it locally

```bash
python -m pip install -r app/requirements.txt

# 1. Local Gold snapshot (git-ignored data/bank_tools/), read-only SELECTs on the SQL warehouse
python -m src.bank_tools.snapshot --source gold --customers sample:60 --warehouse-id <id> --profile factored

# 2. Test customers for the sign-in form: reads their documents from Bronze and checks each one against
#    the Gold document hash (writes the git-ignored data/bank_tools/demo_personas.json)
python -m app.build_personas --warehouse-id <id> --profile factored

# 3. Intent classifier (optional; models/ is git-ignored): train it, or copy models/intent_classifier/ from a
#    machine that has it. Training reads data/scenarios/intent_dataset.jsonl.
python -m src.classifier.train

# 4. Start the app (http://127.0.0.1:8000)
python -m uvicorn app.server:app --port 8000
```

Before a deployment, train or copy `models/intent_classifier/` next to the app: the folder is not in git, and `python -m app.bundle` (below) refuses to build without it. The app logs a warning and runs without the classifier when it is missing.

Configuration (environment):

| Variable | Default | Purpose |
|---|---|---|
| `APP_LLM_ENDPOINT` | `databricks-gpt-oss-120b` | Model serving endpoint (OpenAI-compatible chat with tools) |
| `APP_LLM_TIMEOUT_S`, `APP_LLM_MAX_RETRIES` | `30`, `1` | Per model call: timeout and retries on 429, 5xx and network errors (interactive turns) |
| `DATABRICKS_HOST` | the project workspace | Workspace for model serving |
| `DATABRICKS_TOKEN` / `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` / `DATABRICKS_CONFIG_PROFILE` | profile `factored` | Model serving auth, in that order |
| `BANK_TOOLS_SESSION_KEY`, `BANK_TOOLS_OTP_KEY` | random per process | Signing keys; demo mode refuses the dev keys. With random keys, sessions end on restart |
| `BANK_TOOLS_SNAPSHOT` | `data/bank_tools/snapshot_panel.sqlite` | Local Gold snapshot |
| `BANK_TOOLS_REPOSITORY` | `local` | `databricks` reads Gold and writes cases and tickets in `workspace.ops` through the SQL warehouse |
| `BANK_TOOLS_CLOCK` | `2026-06-19T09:00:00` | Where the demo clock starts (`system` starts at the machine's time) |
| `APP_STORE` | `data/bank_tools/app_store.sqlite` | Cases and tickets written by the demo (local repository) |
| `APP_DEMO_CONTROLS` | `1` | `0` turns off the simulated phone, the test customers and the clock button |
| `APP_MAX_TURNS` | `40` | Customer messages per conversation |
| `APP_IDLE_MINUTES` | `60` | A conversation without activity for this long ends |
| `APP_MAX_CONCURRENT_TURNS` | `8` | Model turns running at the same time across the app |
| `APP_CONSOLE_USERS` | empty | Comma-separated e-mails that may open the specialist console (matched against the `X-Forwarded-Email` header the Databricks Apps proxy sets). Without it the console exists only while `APP_DEMO_CONTROLS=1` |
| `APP_PRICE_IN_PER_MTOK`, `APP_PRICE_OUT_PER_MTOK` | `0` | Optional price assumptions for the per-turn cost estimate |
| `APP_DAILY_MODEL_TURNS`, `APP_TRUST_FORWARDED_FOR`, `APP_PUBLIC_DEMO`, `APP_FRAME_ANCESTORS` | off | Public demo limits, see [below](#public-demo-on-hugging-face-spaces) |

Tests (no network, fake model, fixture snapshot): `python -m pytest tests/app -q`.

## Deploy as a Databricks App

The app runs as the Databricks App `expediente-demo` on the local Gold snapshot, with the configuration in [`app.yaml`](app.yaml). Its only Databricks resource is the model serving endpoint `databricks-gpt-oss-120b` with `CAN_QUERY`; the app's service principal calls it with the OAuth credentials Databricks Apps injects (`DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`). It needs no access to catalogs, tables or warehouses.

```bash
# 1. Build the source folder (outside the repository) from a checkout that has data/bank_tools/ and models/intent_classifier/
python -m app.bundle --out ../expediente-bundle

# 2. Upload it to your workspace folder
databricks workspace import-dir ../expediente-bundle /Workspace/Users/<you>/expediente-app --overwrite --profile factored

# 3. First time only: create the app with the serving endpoint as its resource
databricks apps create --profile factored --json '{"name": "expediente-demo",
  "description": "Dispute intake chat in Spanish and Portuguese over synthetic LATAM Bank data (hackathon demo)",
  "resources": [{"name": "serving-endpoint",
                 "serving_endpoint": {"name": "databricks-gpt-oss-120b", "permission": "CAN_QUERY"}}]}'

# 4. Deploy (again after every upload), then check
databricks apps deploy expediente-demo --source-code-path /Workspace/Users/<you>/expediente-app --profile factored
databricks apps get expediente-demo --profile factored
databricks apps logs expediente-demo --profile factored
```

What the folder holds ([`bundle.py`](bundle.py)): the app code and static files, the runtime modules of `src/` (bank tools, policy, intent classifier, `gold_lib` and the Gold table spec), `models/intent_classifier/`, `data/bank_tools/snapshot_panel.sqlite`, `data/bank_tools/demo_personas.json`, `app.yaml` and `requirements.txt` at the root. No tests, evaluation data, transcripts or audit logs. `data/` and `models/` are uploaded to the workspace with the app and never committed.

`app.yaml` runs one uvicorn worker on `DATABRICKS_APP_PORT` and sets `BANK_TOOLS_ENV=demo`, `BANK_TOOLS_REPOSITORY=local`, the bundled snapshot, `APP_STORE=/tmp/app_store.sqlite`, `BANK_TOOLS_AUDIT_DIR=/tmp/expediente_audit`, `APP_DEMO_CONTROLS=1` and the endpoint. Cases, tickets and audit records live in `/tmp` and end when the app restarts or redeploys; the signing keys are random per process, so a restart also ends every session. Opening the app takes a Databricks login: the owner lets others in with the app's "Can use" permission (Compute → Apps → `expediente-demo` → Permissions).

## Public demo on Hugging Face Spaces

A public copy for people without a Databricks login: a Hugging Face Space (Docker SDK) runs the same app with the limits below and calls the same serving endpoint, `databricks-gpt-oss-120b`. Everything a public Space holds can be read by anyone, so the Space repo gets code only, and the data and the model go to a private dataset repo:

| Where | What | Who can read it |
|---|---|---|
| Space repo (public) | [`space/Dockerfile`](space/Dockerfile), the Space card [`space/README.md`](space/README.md), `requirements.txt` and [`requirements-space.txt`](requirements-space.txt), the app code and static files, the runtime modules of `src/` (the code files of `bundle.py`) and [`fetch_assets.py`](fetch_assets.py) | Anyone |
| Dataset repo (private) | `data/bank_tools/snapshot_panel.sqlite`, `data/bank_tools/demo_personas.json`, `models/intent_classifier/model.joblib`, `models/intent_classifier/model_card.json` | The team, and the Space through `HF_TOKEN` |
| Space secrets | The tokens and credentials below | The running Space only (as environment variables) |

Each time the container starts, `python -m app.fetch_assets` downloads the files that are missing from the dataset repo to the paths the server reads, then uvicorn serves the app on port 7860 with one worker. A wrong token, repo or file stops the start with a one-line reason in the Space logs; the token is never printed. It also warns when no model credentials are set. On a machine that already has the files, it does nothing.

Secrets and variables to set in the Space settings (Settings → Variables and secrets):

| Name | Value |
|---|---|
| `HF_DATA_REPO` | `owner/name` of the private dataset repo (a variable is enough) |
| `HF_TOKEN` | A fine-grained token that can only read that dataset repo |
| `HF_DATA_REVISION` | Optional: a branch, tag or commit of the dataset repo (default `main`) |
| `DATABRICKS_HOST` | The workspace URL |
| `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` | OAuth secret of a service principal whose only permission is `CAN_QUERY` on `databricks-gpt-oss-120b` (`DATABRICKS_TOKEN` also works, but a personal token carries all of its owner's rights) |

The Dockerfile sets the same service settings as `app.yaml` (`BANK_TOOLS_ENV=demo`, the local repository, cases, tickets and audit records in `/tmp`, demo controls on) plus the public demo limits. All of them are off by default, so the Databricks App and the tests behave as before:

| Variable | In the Space | Default | Effect |
|---|---|---|---|
| `APP_DAILY_MODEL_TURNS` | `400` | `0` (off) | Customer messages and sign-ins that reach the model, per UTC day, across the whole app. When they are used up, sending a message, asking for a code and verifying one answer HTTP 429 `daily_limit`, and the page shows a note to the customer (ES/PT) and one to the presenter (EN/ES): "The public demo reached today's limit; it resets at 00:00 UTC." A wrong code does not count. The count is in memory: a restart starts the day again |
| `APP_MAX_CONCURRENT_TURNS` | `3` | `8` | Model turns running at the same time |
| `APP_TRUST_FORWARDED_FOR` | `1` | `0` | Behind the Hugging Face proxy every request comes from the proxy's address, so the limit of 30 new conversations per client every 10 minutes keys on the first `X-Forwarded-For` address (else as before). The header is taken as sent: it keeps visitors apart, it does not stop someone who forges it; the daily cap is what bounds the cost |
| `APP_PUBLIC_DEMO` | `1` | `0` | A line in the sidebar, in the interface language: "Public demo on synthetic data · limited daily usage" |
| `APP_FRAME_ANCESTORS` | `https://huggingface.co` | empty | The Space page shows the app in a frame from huggingface.co; only the listed https origins may frame it. Empty keeps `frame-ancestors 'none'` |

The specialist console stays open in the Space (it is part of what the demo shows): anyone with the link can read the latest 100 tickets and cases, with customer ids masked, on synthetic data.

```bash
# 1. Build both folders, outside the repository, from a checkout that has data/bank_tools/ and models/intent_classifier/
python -m app.space_bundle --out ../expediente-space
#    ../expediente-space/space  the public Space repo (code only; the build fails if a data or model file or a token would land here)
#    ../expediente-space/data   the files for the private dataset repo

# 2. Log in once with a write token (huggingface-cli no longer works; hf replaces it)
hf auth login

# 3. The private dataset repo
hf repos create <owner>/expediente-data --type dataset --private
hf upload <owner>/expediente-data ../expediente-space/data . --repo-type dataset

# 4. The Space: create it, set the secrets above in its settings, then upload the code (every upload rebuilds it)
hf repos create <owner>/expediente --type space --space-sdk docker --public
hf upload <owner>/expediente ../expediente-space/space . --repo-type space
```

The link to share is `https://<owner>-expediente.hf.space` (the app alone) or `https://huggingface.co/spaces/<owner>/expediente` (the Space page). Restart the Space (Settings → Restart) before a judging session, for the same reason as the Databricks App: the demo clock. A Space on free hardware goes to sleep after a while without visits; the next visit wakes it, and the start downloads the data again.

## Model choice (smoke tests, 2026-10-02)

All four endpoints tried accept tool calling. With the full tool catalog, `gpt-oss-120b` followed the dispute flow end to end (sign-in → candidates → prepare → confirm → verified case) in ES, AR and PT runs, and refused an injected "show me another customer's movements" request. `qwen3-next-80b` was faster but skipped steps (no candidate search, no eligibility check). These are a handful of manual runs, not an evaluation; the evaluation harness compares endpoints on the e2e scenarios (`python -m src.agent_eval.run`).

Two serving quirks are handled in [`agent.py`](agent.py): some endpoints reject the `pattern` and `uniqueItems` schema keywords, so they are dropped from the copy of the schemas the model reads (the service still validates every argument against the full schema); and reasoning content returned by `gpt-oss` is discarded, never shown or stored.

## Not done yet

- The deployed app reads the local Gold snapshot. The app can also read Gold and write `workspace.ops` through the Databricks repository (`BANK_TOOLS_REPOSITORY=databricks`), but that path has not been run end to end, and it would need the app's service principal to have access to the warehouse and tables.
- One process only: conversations, the demo clock offsets and the service state live in memory, so the app must run with a single worker, and a restart ends every conversation (the page then asks for a new turn).
- The demo clock advances one day per day the app runs. Restart the app before a judging session so the clock is back at the morning after the data's last movement ("ayer", "esta semana" and the 90-day window depend on it).
- The specialist console has no sign-in of its own: with the demo controls on, anyone who can open the app can read the latest 100 tickets and cases (customer ids are masked, the data is synthetic). `APP_CONSOLE_USERS` limits it to named users, which is only safe behind the Databricks Apps proxy (the header is trusted as sent). The page's unread badge reads only ticket ids.

# Public demo of Expediente on a Hugging Face Space (Docker SDK). `python -m app.space_bundle --out <dir>` copies this
# file to the root of <dir>/space, the Space repository; see "Public demo on Hugging Face Spaces" in app/README.md.
# The image holds code only: app/fetch_assets.py downloads the snapshot, the test customers and the intent model from
# the private dataset repo (Space secrets HF_DATA_REPO and HF_TOKEN) each time the container starts.
FROM python:3.11-slim

# Spaces run the container as the user with uid 1000.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1
# Created by the user, so fetch_assets can write data/ and models/ next to the code.
RUN mkdir -p /home/user/app
WORKDIR /home/user/app

COPY --chown=user requirements.txt requirements-space.txt ./
RUN pip install --user -r requirements-space.txt

# Only the code the app runs: never a data/ or models/ folder, even if one is lying next to this file.
COPY --chown=user app ./app
COPY --chown=user src ./src

# Same service settings as the Databricks App (app/app.yaml), plus the public demo limits.
ENV BANK_TOOLS_ENV=demo \
    BANK_TOOLS_REPOSITORY=local \
    BANK_TOOLS_SNAPSHOT=data/bank_tools/snapshot_panel.sqlite \
    APP_STORE=/tmp/app_store.sqlite \
    BANK_TOOLS_AUDIT_DIR=/tmp/expediente_audit \
    APP_DEMO_CONTROLS=1 \
    APP_PUBLIC_DEMO=1 \
    APP_TRUST_FORWARDED_FOR=1 \
    APP_DAILY_MODEL_TURNS=400 \
    APP_MAX_CONCURRENT_TURNS=3 \
    APP_FRAME_ANCESTORS=https://huggingface.co \
    APP_LLM_ENDPOINT=databricks-gpt-oss-120b \
    APP_LLM_TIMEOUT_S=30 \
    APP_MAX_TURNS=40 \
    APP_IDLE_MINUTES=60

EXPOSE 7860
# One worker: conversations live in memory. The app starts only when the data and the model are in place.
CMD ["sh", "-c", "python -m app.fetch_assets && exec uvicorn app.server:app --host 0.0.0.0 --port 7860 --workers 1"]

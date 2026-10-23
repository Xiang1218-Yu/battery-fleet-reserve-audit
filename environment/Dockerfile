FROM python:3.12.5-slim-bookworm@sha256:c24c34b502635f1f7c4e99dc09a2cbd85d480b7dcfd077198c6b5af138906390
RUN groupadd --gid 10001 agent \
    && useradd --create-home --uid 10001 --gid 10001 agent
WORKDIR /app
COPY --chown=agent:agent reserve_audit /app/reserve_audit
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
# Harbor starts the environment before the agent edits the workspace. Keep it alive;
# the verifier invokes `python3 -m reserve_audit --input ... --output ...` explicitly.
CMD ["sh", "-c", "while :; do sleep 3600; done"]

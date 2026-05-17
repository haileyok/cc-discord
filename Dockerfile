FROM python:3.12-slim

# ── System dependencies ─────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential pkg-config libssl-dev \
      curl sudo ca-certificates gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ── Zellij (pinned, static musl binary) ─────────────────────────────
ARG ZELLIJ_VERSION=0.44.2
ARG TARGETARCH
RUN ZELLIJ_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "aarch64" || echo "x86_64") && \
    curl -fsSL \
      "https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-${ZELLIJ_ARCH}-unknown-linux-musl.tar.gz" \
    | tar xz -C /usr/local/bin && \
    chmod +x /usr/local/bin/zellij

# ── uv (static binary from official image) ──────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# ── Claude CLI ───────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Non-root user ───────────────────────────────────────────────────
RUN useradd -m -s /bin/bash claude

# ── Rust toolchain (owned by claude user) ────────────────────────────
USER claude
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/home/claude/.cargo/bin:${PATH}"
USER root

# ── Application code ────────────────────────────────────────────────
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev && uv cache clean

COPY src/ ./src/
COPY hooks/ ./hooks/
COPY skills/ ./skills/
COPY docker-entrypoint.sh ./

# ── Directory structure & ownership ─────────────────────────────────
RUN mkdir -p \
      /workspace \
      /home/claude/.claude/skills/ask-discord \
      /home/claude/.claude/skills/ask-bridge \
      /home/claude/.config/cc-bridge \
      /home/claude/.local/state/cc-bridge/task-settings \
      /home/claude/.local/state/cc-bridge/attachments && \
    ln -sf /app/skills/SKILL.md /home/claude/.claude/skills/ask-discord/SKILL.md && \
    ln -sf /app/skills/SKILL.md /home/claude/.claude/skills/ask-bridge/SKILL.md && \
    chown -R claude:claude /app /workspace /home/claude && \
    chmod +x /app/docker-entrypoint.sh

# ── Pre-seed Claude Code config (skip onboarding) ───────────────────
RUN echo '{"hasCompletedOnboarding":true,"lastOnboardingVersion":"0.0.0","autoUpdates":false}' \
      > /home/claude/.claude.json && \
    chown claude:claude /home/claude/.claude.json

VOLUME ["/workspace", \
        "/home/claude/.claude", \
        "/home/claude/.config/cc-bridge", \
        "/home/claude/.local/state/cc-bridge"]

EXPOSE 8787

USER claude

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "cc-bridge", "serve", "--host", "127.0.0.1", "--port", "8787"]

FROM debian:bookworm-slim

RUN --mount=type=cache,id=minimax-apt,target=/var/cache/apt \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    curl ca-certificates jq git sudo locales python3 python3-pip \
    xterm x11-utils netpbm netcat-openbsd sqlite3 uuid-runtime openssh-client pandoc rsync lsof procps iproute2 file unzip dnsutils iputils-ping \
    ffmpeg libcairo2-dev libpango1.0-dev pkg-config libffi-dev gcc g++ python3-dev tmux && \
    rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/*

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages fastapi uvicorn pydantic httpx

# Install Node.js (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install OpenAI Codex CLI globally
RUN npm i -g @openai/codex

# Set up X11 resources
ENV DISPLAY=:0

# Used by spawn.sh mount-namespace isolation.
RUN mkdir -p /app

# Create agent user for running spawned agents (idempotent for rebuilds)
RUN id -u agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent \
    && grep -qE '^agent ALL=\(ALL\) NOPASSWD: ALL$' /etc/sudoers || echo "agent ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

COPY agents.py /agents.py
COPY spawn.sh /spawn.sh
COPY agent-runner.sh /agent-runner.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /spawn.sh /agent-runner.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "agents:app", "--host", "0.0.0.0", "--port", "8000"]

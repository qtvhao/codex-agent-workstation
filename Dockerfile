FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    xauth \
    xvfb \
    xterm \
    python3 \
    python3-pip \
    sudo \
    git \
    jq \
    && rm -rf /var/lib/apt/lists/*

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

# Create agent user for running spawned agents
RUN useradd -m agent && echo "agent ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

COPY agents.py /agents.py
COPY spawn.sh /spawn.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /spawn.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "agents:app", "--host", "0.0.0.0", "--port", "8000"]

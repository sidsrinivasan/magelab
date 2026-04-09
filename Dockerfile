FROM python:3.12-bookworm

# ---- System deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Node.js 24 LTS (for Claude CLI) ----
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ---- Claude CLI ----
RUN npm install -g @anthropic-ai/claude-code

# ---- uv ----
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# ---- Install Python to a shared location (not /root/) so --user works ----
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python

# ---- Copy source ----
COPY . /opt/magelab/

RUN cd /opt/magelab && uv sync --frozen --no-dev

# ---- Build frontend dashboard ----
RUN cd /opt/magelab/frontend && npm ci && npx vite build

# ---- Make everything readable/writable by non-root (container runs as host user) ----
RUN chmod -R a+rX /opt/uv/python && chmod -R a+rwX /opt/magelab/.venv

# ---- Install pip into the venv so agent `pip install` goes to the same site-packages ----
RUN /opt/magelab/.venv/bin/python -m ensurepip && \
    /opt/magelab/.venv/bin/python -m pip install --upgrade pip

# ---- Set venv as default Python so agents, magelab, and eval all share one environment ----
ENV PATH="/opt/magelab/.venv/bin:$PATH"
ENV UV_CACHE_DIR=/tmp/uv_cache
ENV HOME=/tmp

WORKDIR /app

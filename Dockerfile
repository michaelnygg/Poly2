FROM python:3.11-slim

WORKDIR /app

# Build deps for crypto/secp256k1 libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    pkg-config \
    libsecp256k1-dev \
    libgmp-dev \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (needed by some crypto deps)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Upgrade pip first
RUN pip install --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-u", "bot.py"]

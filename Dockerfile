FROM python:3.12-slim

# System packages (git for tooling, build-essential for native wheels)
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Interactive login shell by default (dev container keeps it alive via compose)
CMD ["/bin/bash", "-l"]

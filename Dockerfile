# Track the production server's Python MINOR version. The dev container ran 3.12 while the server
# ran 3.14, which meant every verification of stdlib behaviour was an extrapolation — and ISSUE_73
# turned on exactly that kind of detail (`urllib` assigning `req.timeout` before its request
# processors, so a handler can override it). Verified on 3.14.2: assumptions hold, all deps resolve,
# full suite green. The tag floats across patch releases on purpose — matching the minor version is
# what keeps stdlib behaviour comparable; chasing the patch buys false precision and stale CVEs.
FROM python:3.14-slim

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

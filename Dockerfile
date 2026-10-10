# Track the production server's Python version — the PATCH, not only the minor. The dev container ran
# 3.12 while the server ran 3.14, which meant every verification of stdlib behaviour was an
# extrapolation — and ISSUE_73 turned on exactly that kind of detail (`urllib` assigning `req.timeout`
# before its request processors, so a handler can override it).
# The tag used to float across patches on the theory that the minor alone keeps behaviour comparable.
# It does not: 3.14.0–3.14.4 shipped an incremental garbage collector that 3.14.5 reverted, and with
# dev floating to 3.14.7 while the server sat on 3.14.2, the engine grew to 7.2 GB in production and
# the growth could not be reproduced here (2026-09-30). So the patch is pinned to what the server runs
# and bumped together with it (docs/development/running_as_a_service.md, "The interpreter is part of
# the deploy"). The Debian suffix pins the OS base as well; the image tag itself is still rebuilt
# upstream for OS security fixes.
FROM python:3.14.7-slim-trixie

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

"""The bearer-token dependency guarding every non-public route (ISSUE_98)."""
import logging
from typing import Callable, Optional

from fastapi import HTTPException, Request

from finiexragengine.api.rate_limiter import RateLimiter, client_key
from finiexragengine.api.token_registry import TokenRegistry

logger = logging.getLogger(__name__)

# One message for every rejection. A body that distinguished "no header" from "unknown token"
# would answer a question the caller has no right to ask, and the distinction is exactly what a
# guesser probes for.
_DENIED = 'Not authenticated'


def _unauthorized() -> HTTPException:
    # `WWW-Authenticate` is what makes the 401 well-formed: it tells a conforming client which
    # scheme to retry with, and the consumer distinguishes a dead credential from a transport
    # failure by this status rather than by guessing (ISSUE_9 §3.6).
    return HTTPException(status_code=401, detail=_DENIED,
                         headers={'WWW-Authenticate': 'Bearer'})


def build_bearer_dependency(registry: TokenRegistry,
                            limiter: Optional[RateLimiter] = None) -> Callable[[Request], None]:
    """The dependency mounted on the protected router — never on individual routes.

    Mounting it on the `APIRouter` is the whole design: a route added later inherits it by
    construction, so the failure this issue exists to prevent — an endpoint shipped unprotected by
    omission — cannot be reached by forgetting something. `tests/api/test_api_auth.py` asserts that on
    a route registered inside the test.

    `limiter`, when given, bounds **failed** attempts per client: a valid call is never throttled
    here, so a busy consumer cannot rate-limit itself by working.
    """

    def require_bearer(request: Request) -> None:
        header = request.headers.get('authorization', '')
        scheme, _, presented = header.partition(' ')
        consumer = (registry.verify(presented.strip())
                    if scheme.lower() == 'bearer' and presented.strip() else None)
        if consumer is None:
            if limiter is not None:
                key = client_key(request.headers.get('x-forwarded-for'),
                                 request.client.host if request.client else None)
                if not limiter.allow(key):
                    # Deliberately 429 rather than another 401: the caller has stopped being a
                    # failed login and started being traffic, and an operator reading the log
                    # should see the difference.
                    raise HTTPException(status_code=429, detail='Too many attempts')
            # The path is logged, the credential never — not even truncated. A prefix in a log
            # file is a prefix an attacker with the log file no longer has to guess.
            logger.warning('[AUTH] rejected %s %s', request.method, request.url.path)
            raise _unauthorized()
        # Attribution without the secret: downstream logging can name who called.
        request.state.consumer = consumer

    return require_bearer

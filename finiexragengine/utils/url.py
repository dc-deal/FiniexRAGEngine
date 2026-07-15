"""URL helpers — pure string work, no engine dependencies.

Lives here rather than in the unit that happened to need it first: `normalize_host` is used by
the source-health store (grouping rows) *and* the ingestor (stamping a poll), and neither should
have to import the other to normalise a hostname.
"""
from urllib.parse import urlparse


def normalize_host(url: str) -> str:
    """Bare hostname for grouping — lowercased, `www.` stripped ('' if unparseable)."""
    host = (urlparse(url).netloc or '').lower()
    if '@' in host:            # strip any userinfo
        host = host.split('@', 1)[1]
    if ':' in host:            # strip a port
        host = host.split(':', 1)[0]
    return host[4:] if host.startswith('www.') else host

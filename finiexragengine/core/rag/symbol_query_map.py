"""Symbol → retrieval-query mapping — a raw ticker embeds poorly (ISSUE_5)."""
from typing import Dict


class SymbolQueryMap:
    """Resolves a trading symbol to the text embedded as its retrieval query.

    The constellation supplies an alias per symbol (`symbol_queries`, e.g.
    BTCUSD → 'Bitcoin BTC'); the fallback derives the base currency from the
    symbol by stripping a known quote suffix (BTCUSD → 'BTC'). A symbol with
    no alias and no known quote suffix is used as-is.
    """

    # Longest codes first, so BTCUSDT strips 'USDT' before 'USD' matches.
    _QUOTE_CODES = ('USDT', 'USDC', 'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'CAD', 'NZD')

    def __init__(self, aliases: Dict[str, str]) -> None:
        self._aliases = aliases

    def query_for(self, symbol: str) -> str:
        """Return the retrieval query text for `symbol`.

        Args:
            symbol: Trading symbol as configured in the constellation.

        Returns:
            The configured alias, else the derived base currency, else the
            symbol itself.
        """
        alias = self._aliases.get(symbol)
        if alias:
            return alias
        for quote in self._QUOTE_CODES:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return symbol[:-len(quote)]
        return symbol

"""Unit tests for SymbolQueryMap — alias hit, base-currency fallback, pass-through."""
from finiexragengine.core.rag.symbol_query_map import SymbolQueryMap


def test_alias_from_constellation_wins():
    mapping = SymbolQueryMap({'BTCUSD': 'Bitcoin BTC'})
    assert mapping.query_for('BTCUSD') == 'Bitcoin BTC'


def test_fallback_strips_known_quote_suffix():
    mapping = SymbolQueryMap({})
    assert mapping.query_for('EURUSD') == 'EUR'
    assert mapping.query_for('ETHEUR') == 'ETH'


def test_fallback_prefers_longest_quote_code():
    assert SymbolQueryMap({}).query_for('BTCUSDT') == 'BTC'


def test_unknown_symbol_passes_through():
    assert SymbolQueryMap({}).query_for('SPX') == 'SPX'


def test_pure_quote_code_symbol_is_not_emptied():
    assert SymbolQueryMap({}).query_for('USD') == 'USD'

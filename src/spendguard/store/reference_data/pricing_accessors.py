"""Accessor bridge for cyclic consumers that need pricing cost math and constants without importing
``pricing`` and re-creating the cycle. Pricing wires its real functions during its eager import."""

_batch_cost = None
_realtime_cost = None
_price = None
_cache_write_5m_multiplier = None
_cache_write_1h_multiplier = None
_one_million = None
_price_unknown_error = None
_cost_or_unpriced = None
_normalize = None
_pricing_source = None
_pricing_verified = None


def set_pricing_accessors(batch_cost, realtime_cost, price, cache_write_5m_multiplier,
                          cache_write_1h_multiplier, one_million, price_unknown_error,
                          cost_or_unpriced, normalize, pricing_source, pricing_verified):
    """Injected once by pricing at its import with its cost math, constants, and unpriced-model error."""
    global _batch_cost, _realtime_cost, _price, _cache_write_5m_multiplier
    global _cache_write_1h_multiplier, _one_million, _price_unknown_error
    global _cost_or_unpriced, _normalize, _pricing_source, _pricing_verified
    _batch_cost = batch_cost
    _realtime_cost = realtime_cost
    _price = price
    _cache_write_5m_multiplier = cache_write_5m_multiplier
    _cache_write_1h_multiplier = cache_write_1h_multiplier
    _one_million = one_million
    _price_unknown_error = price_unknown_error
    _cost_or_unpriced = cost_or_unpriced
    _normalize = normalize
    _pricing_source = pricing_source
    _pricing_verified = pricing_verified


def priced_batch_cost(model, in_tok, out_tok=0, cached_in_tok=0, provider=None, cache_creation_tok=0):
    if _batch_cost is None:
        raise RuntimeError("import spendguard before calculating batch cost so pricing can wire its accessors")
    return _batch_cost(model, in_tok, out_tok, cached_in_tok, provider, cache_creation_tok)


def priced_realtime_cost(model, in_tok, out_tok=0, cached_in_tok=0, provider=None, cache_creation_tok=0):
    if _realtime_cost is None:
        raise RuntimeError("import spendguard before calculating realtime cost so pricing can wire its accessors")
    return _realtime_cost(model, in_tok, out_tok, cached_in_tok, provider, cache_creation_tok)


def priced_model_rates(model, provider=None, *, as_of=None):
    if _price is None:
        raise RuntimeError("import spendguard before reading a price so pricing can wire its accessors")
    return _price(model, provider, as_of=as_of)


def cache_write_5m_multiplier():
    if _cache_write_5m_multiplier is None:
        raise RuntimeError("import spendguard before reading the five-minute cache-write multiplier so pricing can wire its accessors")
    return _cache_write_5m_multiplier


def cache_write_1h_multiplier():
    if _cache_write_1h_multiplier is None:
        raise RuntimeError("import spendguard before reading the one-hour cache-write multiplier so pricing can wire its accessors")
    return _cache_write_1h_multiplier


def one_million():
    if _one_million is None:
        raise RuntimeError("import spendguard before reading the pricing unit so pricing can wire its accessors")
    return _one_million


def price_unknown_error_type():
    if _price_unknown_error is None:
        raise RuntimeError("import spendguard before reading the unpriced-model error type so pricing can wire its accessors")
    return _price_unknown_error


def priced_cost_or_unpriced(model, in_tok, out_tok=0, cached_in_tok=0, batch=True,
                            provider=None, cache_creation_tok=0):
    if _cost_or_unpriced is None:
        raise RuntimeError("import spendguard before calculating a possibly-unpriced cost so pricing can wire its accessors")
    return _cost_or_unpriced(model, in_tok, out_tok, cached_in_tok, batch, provider, cache_creation_tok)


def normalize_priced_model_name(model):
    if _normalize is None:
        raise RuntimeError("import spendguard before normalizing a priced model name so pricing can wire its accessors")
    return _normalize(model)


def pricing_source_url():
    if _pricing_source is None:
        raise RuntimeError("import spendguard before reading the pricing source URL so pricing can wire its accessors")
    return _pricing_source


def pricing_verified_date():
    if _pricing_verified is None:
        raise RuntimeError("import spendguard before reading the pricing verification date so pricing can wire its accessors")
    return _pricing_verified

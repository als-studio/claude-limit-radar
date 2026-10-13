import pricing


def test_exact_prefix_wins_over_short_family_prefix():
    """claude-opus-4-8 (актуальная, 5/25) не должен получить цену claude-opus-4 (снята, 15/75)."""
    p8 = pricing.price_for("claude-opus-4-8")
    p_old = pricing.price_for("claude-opus-4")
    assert p8 == (5.0, 25.0, 6.25, 10.0, 0.50)
    assert p_old == (15.0, 75.0, 18.75, 30.0, 1.50)
    assert p8 != p_old


def test_dated_suffix_matches_prefix():
    """claude-haiku-4-5-20251001 находит запись claude-haiku-4-5."""
    assert pricing.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0, 1.25, 2.0, 0.10)


def test_unknown_model_returns_none():
    assert pricing.price_for("k3") is None
    assert pricing.price_for("deepseek-v4-pro") is None
    assert pricing.price_for("<synthetic>") is None
    assert pricing.price_for(None) is None


def test_family_groups_by_substring():
    assert pricing.family("claude-opus-4-8") == "opus"
    assert pricing.family("claude-sonnet-5") == "sonnet"
    assert pricing.family("claude-haiku-4-5-20251001") == "haiku"
    assert pricing.family("k3") is None


def _row(model, **kw):
    base = dict(model=model, input=0, output=0, cache_read=0, cache_write=0,
                cache_5m=0, cache_1h=0)
    base.update(kw)
    return base


def test_cost_uses_ttl_split_for_cache_write():
    row = _row("claude-sonnet-5", cache_5m=1_000_000, cache_1h=1_000_000)
    comps = pricing.cost_components(row)
    assert comps["cache_write"] == 2.50 + 4.0    # 1M * $2.50 + 1M * $4.0


def test_cost_falls_back_to_5m_price_when_split_is_zero():
    """Старые записи без разбивки TTL: вся cache_write по цене 5-минутного кэша."""
    row = _row("claude-sonnet-5", cache_write=1_000_000, cache_5m=0, cache_1h=0)
    comps = pricing.cost_components(row)
    assert comps["cache_write"] == 2.50


def test_cost_of_row_sums_components():
    row = _row("claude-sonnet-5", input=1_000_000, output=1_000_000,
               cache_read=1_000_000, cache_5m=1_000_000)
    assert pricing.cost_of_row(row) == 2.0 + 10.0 + 0.20 + 2.50


def test_cost_of_row_none_for_unpriced_model():
    assert pricing.cost_of_row(_row("k3", input=1_000_000)) is None


def test_explicit_price_overrides_lookup():
    """Для контрфактических расчётов (Д2: 'сколько стоило бы на Sonnet')."""
    row = _row("claude-opus-5", output=1_000_000)
    sonnet_price = pricing.price_for(pricing.CHEAPEST_CURRENT_MODEL)
    assert pricing.cost_of_row(row, price=sonnet_price) == 10.0    # цена Sonnet, не Opus


def test_breakdown_sums_categories_and_flags_unpriced():
    rows = [
        _row("claude-sonnet-5", input=1_000_000),
        _row("claude-opus-5", output=1_000_000),
        _row("k3", output=1_000_000),
        _row("deepseek-v4-pro", output=1_000_000),
    ]
    b = pricing.cost_breakdown(rows)
    assert b["by_category"]["input"] == 2.0
    assert b["by_category"]["output"] == 25.0
    assert round(b["total"], 6) == 27.0
    assert b["unpriced_n"] == 2
    assert b["unpriced_models"] == ["deepseek-v4-pro", "k3"]


def test_breakdown_on_empty_list():
    b = pricing.cost_breakdown([])
    assert b["total"] == 0.0
    assert b["unpriced_n"] == 0

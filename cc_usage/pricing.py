"""Оценка стоимости запросов по публичному прайсу Anthropic. Только stdlib.

Прайс закреплён на PRICES_AS_OF, сверен 2026-08-20 с
platform.claude.com/docs/en/about-claude/pricing. Цена привязана к точному
имени модели (самый длинный совпадающий префикс), а не к семейству: внутри
одного семейства сосуществуют версии с разной ценой — Opus 4/4.1 (сняты с
продажи) стоят втрое дороже актуальных Opus 4.5-5, Sonnet 5 дешевле
Sonnet 4.x. Стоимость не материализуется в БД — считается на лету, поэтому
правка этого файла бесплатно пересчитывает всю историю.
"""

# $ за миллион токенов: (input, output, cache_write_5m, cache_write_1h, cache_read)
PRICES = {
    "claude-opus-5":     ( 5.0, 25.0,  6.25, 10.0, 0.50),
    "claude-opus-4-8":   ( 5.0, 25.0,  6.25, 10.0, 0.50),
    "claude-opus-4-7":   ( 5.0, 25.0,  6.25, 10.0, 0.50),
    "claude-opus-4-6":   ( 5.0, 25.0,  6.25, 10.0, 0.50),
    "claude-opus-4-5":   ( 5.0, 25.0,  6.25, 10.0, 0.50),
    "claude-opus-4-1":   (15.0, 75.0, 18.75, 30.0, 1.50),  # снята с продажи
    "claude-opus-4":     (15.0, 75.0, 18.75, 30.0, 1.50),  # снята с продажи;
                                                            # должна идти ПОСЛЕ
                                                            # "claude-opus-4-*"
                                                            # при сопоставлении
    "claude-sonnet-5":   ( 2.0, 10.0,  2.50,  4.0, 0.20),
    "claude-sonnet-4-6": ( 3.0, 15.0,  3.75,  6.0, 0.30),
    "claude-sonnet-4-5": ( 3.0, 15.0,  3.75,  6.0, 0.30),
    "claude-sonnet-4":   ( 3.0, 15.0,  3.75,  6.0, 0.30),
    "claude-haiku-4-5":  ( 1.0,  5.0,  1.25,  2.0, 0.10),
    "claude-haiku-3-5":  ( 0.80, 4.0,  1.00,  1.60, 0.08),  # снята с продажи
    "claude-fable-5":    (10.0, 50.0, 12.50, 20.0, 1.00),
    "claude-mythos-5":   (10.0, 50.0, 12.50, 20.0, 1.00),
}
PRICES_AS_OF = "2026-08-20"

# Самая дешёвая из актуальных (не снятых с продажи) моделей — контрфактическая
# цена для диагноза "дорогая модель" (Д2): чем была бы та же работа на Sonnet.
CHEAPEST_CURRENT_MODEL = "claude-sonnet-5"

# По убыванию длины ключа — самый длинный совпадающий префикс проверяется
# первым, чтобы "claude-opus-4-8" не совпал с записью "claude-opus-4".
_PREFIXES_BY_LENGTH = sorted(PRICES, key=len, reverse=True)

_FAMILY_NAMES = ("opus", "sonnet", "haiku", "fable", "mythos")

_MTOK = 1_000_000.0


def price_for(model):
    """Кортеж цен по самому длинному совпадающему префиксу имени модели.

    None, если модель не найдена в таблице (сторонние модели вроде k3,
    deepseek, <synthetic> — не считаются нулём молча, см. cost_breakdown).
    """
    if not model:
        return None
    for prefix in _PREFIXES_BY_LENGTH:
        if model.startswith(prefix):
            return PRICES[prefix]
    return None


def family(model):
    """Дисплейная группировка (opus/sonnet/haiku/fable/mythos) для таблиц.

    На стоимость не влияет — та всегда считается через price_for точной
    версии модели.
    """
    if not model:
        return None
    for name in _FAMILY_NAMES:
        if name in model:
            return name
    return None


def cost_components(row, price=None):
    """Стоимость запроса по 4 статьям в $, либо None если модель вне прайса.

    price можно передать явно (для контрфактических расчётов вроде Д2:
    "сколько стоило бы на Sonnet"). Если оба поля TTL (cache_5m, cache_1h)
    нулевые, а cache_write не ноль — старая запись без разбивки: вся запись
    считается по цене 5-минутного кэша (fallback, приближённая оценка).
    """
    p = price if price is not None else price_for(row.get("model"))
    if p is None:
        return None
    p_in, p_out, p5m, p1h, p_read = p
    c5m = row.get("cache_5m") or 0
    c1h = row.get("cache_1h") or 0
    cw = row.get("cache_write") or 0
    if c5m == 0 and c1h == 0 and cw > 0:
        write_cost = cw / _MTOK * p5m
    else:
        write_cost = c5m / _MTOK * p5m + c1h / _MTOK * p1h
    return {
        "input": (row.get("input") or 0) / _MTOK * p_in,
        "output": (row.get("output") or 0) / _MTOK * p_out,
        "cache_write": write_cost,
        "cache_read": (row.get("cache_read") or 0) / _MTOK * p_read,
    }


def cost_of_row(row, price=None):
    """Итоговая стоимость запроса в $, либо None если модель вне прайса."""
    comps = cost_components(row, price=price)
    return sum(comps.values()) if comps is not None else None


def cost_breakdown(rows):
    """Разбивка по 4 статьям + итог + счётчик неучтённых моделей."""
    totals = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
    unpriced_n = 0
    unpriced_models = set()
    for r in rows:
        comps = cost_components(r)
        if comps is None:
            unpriced_n += 1
            if r.get("model"):
                unpriced_models.add(r["model"])
            continue
        for k in totals:
            totals[k] += comps[k]
    return {
        "by_category": totals,
        "total": sum(totals.values()),
        "unpriced_n": unpriced_n,
        "unpriced_models": sorted(unpriced_models),
    }

"""Форматирование чисел и текста для отчёта: деньги, токены, проценты, время.

Полные числа остаются доступны в тултипах SVG и в JSON-payload отчёта —
здесь только сокращённое отображение для человека.
"""


def money(x):
    """$12.4K, $734, $0.42, $8.2M"""
    x = float(x or 0)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return "%s$%.1fM" % (sign, x / 1_000_000)
    if x >= 1_000:
        return "%s$%.1fK" % (sign, x / 1_000)
    if x >= 1:
        return "%s$%.0f" % (sign, x)
    return "%s$%.2f" % (sign, x)


def tokens(x):
    """8.95G, 238M, 20K, 734"""
    x = float(x or 0)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000_000:
        return "%s%.2fG" % (sign, x / 1_000_000_000)
    if x >= 1_000_000:
        return "%s%.0fM" % (sign, x / 1_000_000)
    if x >= 1_000:
        return "%s%.0fK" % (sign, x / 1_000)
    return "%s%d" % (sign, x)


def pct(x):
    """58.4%. x — доля 0.0-1.0, не готовые проценты."""
    return "%.1f%%" % (100 * float(x or 0))


def dur(seconds):
    """1 ч 58 мин, 12 мин, 45 сек"""
    s = int(seconds or 0)
    sign = "-" if s < 0 else ""
    s = abs(s)
    if s < 60:
        return "%s%d сек" % (sign, s)
    m = s // 60
    if m < 60:
        return "%s%d мин" % (sign, m)
    h = m // 60
    rem_m = m % 60
    if rem_m:
        return "%s%d ч %d мин" % (sign, h, rem_m)
    return "%s%d ч" % (sign, h)


def title(text, n):
    """Обрезка по границе слова с многоточием, не длиннее n символов."""
    if not text:
        return ""
    text = " ".join(text.split())    # схлопнуть переносы строк и пробелы
    if len(text) <= n:
        return text
    cut = text.rfind(" ", 0, n)
    if cut <= 0:
        cut = n
    return text[:cut].rstrip() + "…"

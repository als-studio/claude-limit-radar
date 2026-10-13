import fmt


def test_money_thresholds():
    assert fmt.money(0.42) == "$0.42"
    assert fmt.money(734) == "$734"
    assert fmt.money(12400) == "$12.4K"
    assert fmt.money(8_200_000) == "$8.2M"
    assert fmt.money(0) == "$0.00"
    assert fmt.money(-5) == "-$5"


def test_tokens_thresholds():
    assert fmt.tokens(734) == "734"
    assert fmt.tokens(20_000) == "20K"
    assert fmt.tokens(238_000_000) == "238M"
    assert fmt.tokens(8_950_000_000) == "8.95G"


def test_pct_takes_fraction_not_percent():
    assert fmt.pct(0.584) == "58.4%"
    assert fmt.pct(0) == "0.0%"
    assert fmt.pct(1) == "100.0%"


def test_dur_thresholds():
    assert fmt.dur(45) == "45 сек"
    assert fmt.dur(12 * 60) == "12 мин"
    assert fmt.dur(60 * 60) == "1 ч"
    assert fmt.dur(60 * 60 + 58 * 60) == "1 ч 58 мин"


def test_title_truncates_on_word_boundary():
    long_text = "Реализовать статистику по тегам для дашборда клиента"
    out = fmt.title(long_text, 20)
    assert out.endswith("…")
    assert len(out) <= 21
    assert not out[:-1].endswith(" ")


def test_title_keeps_short_text_untouched():
    assert fmt.title("Коротко", 80) == "Коротко"


def test_title_handles_empty():
    assert fmt.title("", 80) == ""
    assert fmt.title(None, 80) == ""


def test_title_collapses_whitespace():
    assert fmt.title("строка   с   переносом\nстрок", 80) == "строка с переносом строк"

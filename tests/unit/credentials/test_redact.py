from brain_v42.credentials.redact import short_id


def test_short_id_redacts_long_values() -> None:
    assert short_id("0123456789abcdef") == "01234567…"


def test_short_id_keeps_values_of_eight_characters() -> None:
    assert short_id("12345678") == "12345678"


def test_short_id_marks_missing_values() -> None:
    assert short_id(None) == "-"
    assert short_id("") == "-"

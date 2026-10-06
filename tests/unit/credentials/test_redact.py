from brain_v42.credentials.redact import sanitize_label, short_id


def test_short_id_redacts_long_values() -> None:
    assert short_id("0123456789abcdef") == "01234567…"


def test_short_id_keeps_values_of_eight_characters() -> None:
    assert short_id("12345678") == "12345678"


def test_short_id_marks_missing_values() -> None:
    assert short_id(None) == "-"
    assert short_id("") == "-"


def test_sanitize_label_preserves_missing_and_empty_labels() -> None:
    assert sanitize_label(None) is None
    assert sanitize_label("") == ""


def test_sanitize_label_replaces_every_disallowed_character() -> None:
    assert sanitize_label("abc09.:_*-AZ /é\n") == "abc09.:_*-" + "_" * 6


def test_sanitize_label_bounds_input_before_replacement() -> None:
    assert sanitize_label("a" * 63 + "É" + "z" * 100) == "a" * 63 + "_"

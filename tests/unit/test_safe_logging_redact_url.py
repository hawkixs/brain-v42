from brain_v42.safe_logging import redact_url


def test_redact_url_removes_userinfo_query_and_fragment() -> None:
    assert redact_url("https://user:secret@example.test:8443/path?q=secret#fragment") == (
        "https://example.test:8443/path"
    )


def test_redact_url_preserves_none() -> None:
    assert redact_url(None) is None


def test_redact_url_malformed_input_does_not_raise() -> None:
    assert redact_url("http://[invalid") == "<unparseable url>"


def test_redact_url_invalid_port_does_not_raise() -> None:
    assert redact_url("http://h:abc/") == "<unparseable url>"


def test_redact_url_empty_string_is_unparseable() -> None:
    assert redact_url("") == "<unparseable url>"


def test_redact_url_handles_password_without_username_ipv6_port_and_fragment() -> None:
    assert redact_url("https://:secret@[2001:db8::1]:8443/path#private") == (
        "https://[2001:db8::1]:8443/path"
    )

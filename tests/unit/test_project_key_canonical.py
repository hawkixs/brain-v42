"""Tests for canonicalize_project_key — the single guard against project_key drift.

Background (learning 7bc821a1): the repo/package/registry/GitNexus index are all
named ``brain_v42`` (underscore), but the canonical brain project_key is
``brain-v42`` (hyphen). The write path stored keys verbatim, so the underscore
form silently created a phantom project. This guard canonicalizes the KNOWN
confusables and rejects everything else that is not kebab-case ("both" /
ceinture+bretelles).
"""

import pytest

from brain_v42.models.project_key import canonicalize_project_key


class TestCanonicalizeProjectKey:
    # --- pass-through / None ------------------------------------------------
    def test_none_passes_through(self):
        assert canonicalize_project_key(None) is None

    def test_already_canonical_unchanged(self):
        assert canonicalize_project_key("brain-v42") == "brain-v42"

    def test_simple_kebab_unchanged(self):
        assert canonicalize_project_key("red") == "red"

    def test_colon_subpartition_unchanged(self):
        assert canonicalize_project_key("red-lab:architect") == "red-lab:architect"

    # --- known confusables are canonicalized (alias map) --------------------
    def test_underscore_brain_v42_canonicalized(self):
        assert canonicalize_project_key("brain_v42") == "brain-v42"

    def test_bare_brain_canonicalized(self):
        assert canonicalize_project_key("brain") == "brain-v42"

    def test_alias_is_trimmed(self):
        assert canonicalize_project_key("  brain_v42  ") == "brain-v42"

    def test_uppercase_confusable_lookalike_rejected(self):
        # Aliases are matched case-sensitively: uppercase is non-kebab → rejected
        # (with a hint), never silently accepted.
        with pytest.raises(ValueError, match="brain-v42"):
            canonicalize_project_key("BRAIN_V42")

    # --- everything else non-kebab is REJECTED loudly -----------------------
    def test_generic_underscore_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            canonicalize_project_key("red_data")

    def test_generic_underscore_hint_suggests_hyphen(self):
        with pytest.raises(ValueError, match="red-data"):
            canonicalize_project_key("red_data")

    def test_uppercase_non_alias_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            canonicalize_project_key("RED-DATA")

    def test_space_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            canonicalize_project_key("my project")

    def test_trailing_hyphen_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            canonicalize_project_key("brain-")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="kebab-case"):
            canonicalize_project_key("")


class TestLenientMode:
    """Reads are forgiving: fix the known confusable, pass anything else through
    untouched (a bad lookup just yields no results), never raise."""

    def test_confusable_still_canonicalized(self):
        assert canonicalize_project_key("brain_v42", strict=False) == "brain-v42"

    def test_bare_brain_still_canonicalized(self):
        assert canonicalize_project_key("brain", strict=False) == "brain-v42"

    def test_unknown_underscore_passes_through(self):
        assert canonicalize_project_key("unknown_proj", strict=False) == "unknown_proj"

    def test_uppercase_passes_through(self):
        assert canonicalize_project_key("RED-DATA", strict=False) == "RED-DATA"

    def test_none_passes_through(self):
        assert canonicalize_project_key(None, strict=False) is None

    def test_canonical_unchanged(self):
        assert canonicalize_project_key("brain-v42", strict=False) == "brain-v42"

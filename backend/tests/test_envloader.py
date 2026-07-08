"""Unit tests for backend/_envloader.py (finding BE11-6).

Covers the documented contracts for every public helper:
  - env_float: NaN guard (P24), inf behavior, min/max clamping, missing var
  - env_int: basic parse, min/max clamping
  - env_bool: whitelist strictness (typos return default, not True)
  - env_str: trim and fallback
  - env_str_set: comma + semicolon separators
  - is_real_secret: case-insensitive placeholder detection
  - env_secret_or_none: placeholder vs real value
  - _fallback_parse: KEY=value=with=equals uses partition (value includes '=')

All tests use monkeypatch to avoid polluting the real os.environ.
No network calls, no disk I/O, no production state touched.
"""
from __future__ import annotations

import math
import os

import pytest

from backend._envloader import (
    _fallback_parse,
    env_bool,
    env_float,
    env_int,
    env_secret_or_none,
    env_str,
    env_str_set,
    is_real_secret,
)


# ---------------------------------------------------------------------------
# env_float
# ---------------------------------------------------------------------------

class TestEnvFloat:
    def test_nan_returns_default(self, monkeypatch):
        """P24: VAR=nan must return default, not propagate NaN (every NaN comparison is False)."""
        monkeypatch.setenv("_TEST_FLOAT", "nan")
        result = env_float("_TEST_FLOAT", 42.0)
        assert result == 42.0
        assert not math.isnan(result)

    def test_nan_uppercase_returns_default(self, monkeypatch):
        """NaN is case-insensitive in float(); NaN, NAN, Nan all must return default."""
        monkeypatch.setenv("_TEST_FLOAT", "NaN")
        result = env_float("_TEST_FLOAT", 99.0)
        assert result == 99.0

    def test_inf_without_maximum_returns_inf(self, monkeypatch):
        """VAR=inf with no maximum set returns math.inf (documents the behavior)."""
        monkeypatch.setenv("_TEST_FLOAT", "inf")
        result = env_float("_TEST_FLOAT", 1.0)
        assert result == math.inf

    def test_inf_with_maximum_is_clamped(self, monkeypatch):
        """VAR=inf with maximum=100.0 must return 100.0 (clamp)."""
        monkeypatch.setenv("_TEST_FLOAT", "inf")
        result = env_float("_TEST_FLOAT", 1.0, maximum=100.0)
        assert result == 100.0

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_FLOAT", raising=False)
        assert env_float("_TEST_FLOAT", 3.14) == 3.14

    def test_valid_float_parsed(self, monkeypatch):
        monkeypatch.setenv("_TEST_FLOAT", "2.718")
        assert env_float("_TEST_FLOAT", 0.0) == pytest.approx(2.718)

    def test_minimum_clamp(self, monkeypatch):
        monkeypatch.setenv("_TEST_FLOAT", "-5.0")
        assert env_float("_TEST_FLOAT", 0.0, minimum=0.0) == 0.0

    def test_maximum_clamp(self, monkeypatch):
        monkeypatch.setenv("_TEST_FLOAT", "200.0")
        assert env_float("_TEST_FLOAT", 0.0, maximum=100.0) == 100.0

    def test_malformed_returns_default(self, monkeypatch):
        monkeypatch.setenv("_TEST_FLOAT", "not_a_float")
        assert env_float("_TEST_FLOAT", 7.0) == 7.0


# ---------------------------------------------------------------------------
# env_int
# ---------------------------------------------------------------------------

class TestEnvInt:
    def test_valid_int_parsed(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT", "42")
        assert env_int("_TEST_INT", 0) == 42

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_INT", raising=False)
        assert env_int("_TEST_INT", 99) == 99

    def test_minimum_clamp(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT", "-10")
        assert env_int("_TEST_INT", 0, minimum=1) == 1

    def test_maximum_clamp(self, monkeypatch):
        monkeypatch.setenv("_TEST_INT", "1000")
        assert env_int("_TEST_INT", 0, maximum=100) == 100

    def test_float_string_returns_default(self, monkeypatch):
        """'3.14' is not a valid int string; must return default."""
        monkeypatch.setenv("_TEST_INT", "3.14")
        assert env_int("_TEST_INT", 5) == 5


# ---------------------------------------------------------------------------
# env_bool: whitelist strictness
# ---------------------------------------------------------------------------

class TestEnvBool:
    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"])
    def test_truthy_tokens(self, monkeypatch, val):
        monkeypatch.setenv("_TEST_BOOL", val)
        assert env_bool("_TEST_BOOL", False) is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "FALSE", "no", "NO", "off", "OFF"])
    def test_falsy_tokens(self, monkeypatch, val):
        monkeypatch.setenv("_TEST_BOOL", val)
        assert env_bool("_TEST_BOOL", True) is False

    @pytest.mark.parametrize("val", ["ture", "tru", "fals", "yes_typo", "enable", "enabled", "2"])
    def test_typo_returns_default(self, monkeypatch, val):
        """Misspelled tokens must return the default, not True (whitelist strictness)."""
        monkeypatch.setenv("_TEST_BOOL", val)
        assert env_bool("_TEST_BOOL", False) is False
        assert env_bool("_TEST_BOOL", True) is True

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_BOOL", raising=False)
        assert env_bool("_TEST_BOOL", True) is True
        assert env_bool("_TEST_BOOL", False) is False


# ---------------------------------------------------------------------------
# env_str
# ---------------------------------------------------------------------------

class TestEnvStr:
    def test_value_returned(self, monkeypatch):
        monkeypatch.setenv("_TEST_STR", "  hello  ")
        assert env_str("_TEST_STR", "default") == "hello"

    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_STR", raising=False)
        assert env_str("_TEST_STR", "fallback") == "fallback"

    def test_whitespace_only_returns_default(self, monkeypatch):
        monkeypatch.setenv("_TEST_STR", "   ")
        assert env_str("_TEST_STR", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# env_str_set
# ---------------------------------------------------------------------------

class TestEnvStrSet:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("_TEST_SET", "a,b,c")
        assert env_str_set("_TEST_SET") == {"a", "b", "c"}

    def test_semicolon_separated(self, monkeypatch):
        monkeypatch.setenv("_TEST_SET", "x;y;z")
        assert env_str_set("_TEST_SET") == {"x", "y", "z"}

    def test_mixed_separators(self, monkeypatch):
        monkeypatch.setenv("_TEST_SET", "a,b;c")
        result = env_str_set("_TEST_SET")
        assert result == {"a", "b", "c"}

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("_TEST_SET", " a , b , c ")
        assert env_str_set("_TEST_SET") == {"a", "b", "c"}

    def test_missing_returns_empty_set(self, monkeypatch):
        monkeypatch.delenv("_TEST_SET", raising=False)
        assert env_str_set("_TEST_SET") == set()

    def test_missing_returns_provided_default(self, monkeypatch):
        monkeypatch.delenv("_TEST_SET", raising=False)
        assert env_str_set("_TEST_SET", {"default"}) == {"default"}

    def test_custom_separator(self, monkeypatch):
        """separators='|' overrides the default ',;' — only '|' splits tokens."""
        monkeypatch.setenv("_TEST_SET_PIPE", "a|b|c")
        assert env_str_set("_TEST_SET_PIPE", separators="|") == {"a", "b", "c"}

    def test_custom_separator_ignores_default_delimiters(self, monkeypatch):
        """When separators='|', commas are literal and must NOT split."""
        monkeypatch.setenv("_TEST_SET_PIPE2", "a,b|c")
        result = env_str_set("_TEST_SET_PIPE2", separators="|")
        assert result == {"a,b", "c"}

    def test_empty_token_filtered(self, monkeypatch):
        """Double-comma produces an empty token; the if-p.strip() guard must drop it."""
        monkeypatch.setenv("_TEST_SET_EMPTY", "a,,b")
        assert env_str_set("_TEST_SET_EMPTY") == {"a", "b"}


# ---------------------------------------------------------------------------
# is_real_secret: placeholder detection (case-insensitive)
# ---------------------------------------------------------------------------

class TestIsRealSecret:
    @pytest.mark.parametrize("val", [
        "your_api_key",
        "YOUR_API_KEY",
        "your_secret_here",
        "xxxxxxxx",
        "XXXXXXXXXXXX",
        "placeholder_key",
        "PLACEHOLDER",
        "changeme",
        "CHANGEME",
    ])
    def test_placeholder_strings_return_false(self, val):
        assert is_real_secret(val) is False

    @pytest.mark.parametrize("val", [
        "sk-realkey1234567890",
        "anthropic-prod-key-abc",
        "ghp_actualtoken",
    ])
    def test_real_secrets_return_true(self, val):
        assert is_real_secret(val) is True

    def test_none_returns_false(self):
        assert is_real_secret(None) is False

    def test_empty_string_returns_false(self):
        assert is_real_secret("") is False

    def test_whitespace_only_returns_false(self):
        assert is_real_secret("   ") is False


# ---------------------------------------------------------------------------
# env_secret_or_none
# ---------------------------------------------------------------------------

class TestEnvSecretOrNone:
    def test_real_value_returned(self, monkeypatch):
        monkeypatch.setenv("_TEST_SECRET", "sk-realkey1234")
        assert env_secret_or_none("_TEST_SECRET") == "sk-realkey1234"

    def test_placeholder_returns_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_SECRET", "your_api_key_here")
        assert env_secret_or_none("_TEST_SECRET") is None

    def test_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("_TEST_SECRET", raising=False)
        assert env_secret_or_none("_TEST_SECRET") is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_SECRET", "")
        assert env_secret_or_none("_TEST_SECRET") is None


# ---------------------------------------------------------------------------
# _fallback_parse: KEY=value=with=equals uses str.partition (value includes '=')
# ---------------------------------------------------------------------------

class TestFallbackParse:
    def test_simple_key_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("_TEST_FB_A=hello\n", encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_A", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_A") == "hello"
        monkeypatch.delenv("_TEST_FB_A", raising=False)

    def test_value_with_equals_uses_partition(self, tmp_path, monkeypatch):
        """KEY=value=extra: partition keeps 'value=extra' as the full value."""
        env_file = tmp_path / ".env"
        env_file.write_text("_TEST_FB_B=value=extra\n", encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_B", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_B") == "value=extra"
        monkeypatch.delenv("_TEST_FB_B", raising=False)

    def test_comment_lines_skipped(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("# This is a comment\n_TEST_FB_C=real\n", encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_C", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_C") == "real"
        monkeypatch.delenv("_TEST_FB_C", raising=False)

    def test_export_prefix_stripped(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("export _TEST_FB_D=exported\n", encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_D", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_D") == "exported"
        monkeypatch.delenv("_TEST_FB_D", raising=False)

    def test_quoted_value_unquoted(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('_TEST_FB_E="quoted_value"\n', encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_E", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_E") == "quoted_value"
        monkeypatch.delenv("_TEST_FB_E", raising=False)

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        """_fallback_parse uses setdefault: existing env vars must not be overwritten."""
        env_file = tmp_path / ".env"
        env_file.write_text("_TEST_FB_F=from_file\n", encoding="utf-8")
        monkeypatch.setenv("_TEST_FB_F", "from_shell")
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_F") == "from_shell"

    def test_missing_file_no_error(self, tmp_path):
        """Non-existent .env file must be silently ignored."""
        _fallback_parse(tmp_path / "nonexistent.env")  # must not raise

    def test_inline_comment_included_in_value(self, tmp_path, monkeypatch):
        """_fallback_parse does NOT strip inline comments — diverges from python-dotenv.

        python-dotenv strips text after an unquoted '#' so `KEY=true # comment`
        stores `'true'`. _fallback_parse only strips *leading* '#' lines and has
        no inline-comment stripping, so `KEY=true # comment` stores
        `'true # comment'` (the full post-'=' text after outer whitespace strip).

        This test documents the current behavior as an explicit contract so that
        any future change to _fallback_parse is visible in the test diff.
        Note: a value like `'true # comment'` is NOT in _TRUE_TOKENS, so
        env_bool() would return its default rather than True — operators must
        not write inline comments in .env files when dotenv is unavailable.
        """
        env_file = tmp_path / ".env"
        env_file.write_text("_TEST_FB_INLINE=true # this is a comment\n", encoding="utf-8")
        monkeypatch.delenv("_TEST_FB_INLINE", raising=False)
        _fallback_parse(env_file)
        assert os.environ.get("_TEST_FB_INLINE") == "true # this is a comment"
        monkeypatch.delenv("_TEST_FB_INLINE", raising=False)

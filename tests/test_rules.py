"""Extraction rules (§6.4 Track 2): the constrained, interpreted format.

The security property under test: a rule is inert data. No field names code, and
malformed or hostile rules are rejected at parse time, not honoured.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawler.rules import ExtractRule, parse_rule


class TestParsing:
    def test_a_minimal_html_rule_parses(self):
        rule = parse_rule({"item_selector": "li.bando"})
        assert rule.item_selector == "li.bando"
        assert rule.is_usable()
        assert not rule.is_api_style()

    def test_an_api_rule_parses(self):
        rule = parse_rule({"results_path": "results", "item_url_path": "url"})
        assert rule.is_api_style()
        assert rule.is_usable()

    def test_an_empty_rule_is_not_usable(self):
        assert not parse_rule({}).is_usable()

    def test_non_object_is_rejected(self):
        with pytest.raises((ValueError, ValidationError)):
            parse_rule("li.bando")


class TestSafety:
    """A rule must not be able to smuggle in anything executable."""

    def test_javascript_selector_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_rule({"item_selector": "a[href=javascript:alert(1)]"})

    def test_script_injection_in_selector_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_rule({"item_selector": "<script>evil()</script>"})

    def test_only_get_and_post_are_allowed(self):
        with pytest.raises(ValidationError):
            parse_rule({"item_selector": "li", "request": {"method": "DELETE"}})

    def test_absurdly_long_selector_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_rule({"item_selector": "x" * 500})

    def test_max_pages_is_bounded(self):
        with pytest.raises(ValidationError):
            parse_rule({"item_selector": "li", "max_pages": 9999})


class TestDefaults:
    def test_request_defaults_to_get(self):
        assert parse_rule({"item_selector": "li"}).request.method == "GET"

    def test_render_defaults_false(self):
        assert parse_rule({"item_selector": "li"}).render is False

    def test_post_method_uppercased(self):
        rule = parse_rule({"results_path": "r", "request": {"method": "post"}})
        assert rule.request.method == "POST"

"""HTML cleaning, and the diagnostics that make its failures visible (§6.5)."""

from __future__ import annotations

import pathlib

from crawler.clean import clean_html

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_keeps_the_grant_content():
    cleaned = clean_html((FIXTURES / "regione_detail.html").read_text())
    assert "Voucher Digitale 4.0 per le PMI" in cleaned.text
    assert "PMI con sede legale o operativa in Italia" in cleaned.text
    assert "€10.000 - €50.000" in cleaned.text


def test_strips_scripts_styles_and_chrome():
    cleaned = clean_html((FIXTURES / "regione_detail.html").read_text())
    assert "analytics" not in cleaned.text
    assert "display: none" not in cleaned.text
    # nav/footer are decomposed, so their link text goes with them.
    assert "Amministrazione trasparente" not in cleaned.text


def test_truncation_is_reported_not_silent():
    """§6.5: 'the cleaner ate it' must be distinguishable from 'the LLM missed it'."""
    html = "<html><body><p>" + ("bando " * 20_000) + "</p></body></html>"
    cleaned = clean_html(html, max_chars=1000)
    assert cleaned.truncated is True
    assert cleaned.text_length == 1000
    assert cleaned.as_diagnostics()["truncated"] is True


def test_short_pages_are_not_marked_truncated():
    cleaned = clean_html("<html><body><p>Bando breve</p></body></html>")
    assert cleaned.truncated is False


def test_diagnostics_carry_enough_to_debug():
    cleaned = clean_html((FIXTURES / "regione_detail.html").read_text())
    diagnostics = cleaned.as_diagnostics()
    assert diagnostics["fetched_bytes"] > 0
    assert diagnostics["cleaned_text_length"] > 0
    # The head is what shows whether the content was ever there.
    assert len(str(diagnostics["cleaned_text_head"])) > 0


def test_js_rendered_shell_yields_empty_text():
    """The most common real failure: content is client-rendered, so httpx sees
    an empty shell. Diagnostics must make that obvious (fix: type -> html_js)."""
    cleaned = clean_html('<html><body><div id="root"></div><script>render()</script></body></html>')
    assert cleaned.text.strip() == ""
    assert cleaned.as_diagnostics()["cleaned_text_length"] == 0

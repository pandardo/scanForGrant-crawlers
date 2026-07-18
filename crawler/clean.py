"""HTML → clean text for the LLM.

This step is a documented failure mode (§6.5): if the cleaner strips or truncates
the content, the LLM sees nothing and reports nothing — indistinguishable from
"the model missed it" unless we record what happened. So cleaning returns its own
diagnostics alongside the text.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser

# Elements that never carry grant content but bulk up the token budget.
NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "nav",
    "footer",
    "header",
    "form",
)


@dataclass(frozen=True)
class CleanedPage:
    text: str
    original_bytes: int
    text_length: int
    truncated: bool

    def as_diagnostics(self) -> dict[str, object]:
        """The subset §6.5 wants recorded on a zero-candidate run."""
        return {
            "fetched_bytes": self.original_bytes,
            "cleaned_text_length": self.text_length,
            "truncated": self.truncated,
            # Enough to see whether the content was ever there.
            "cleaned_text_head": self.text[:500],
        }


def clean_html(html: str, *, max_chars: int = 24_000) -> CleanedPage:
    """Strip markup and noise, collapse whitespace, truncate to a token budget.

    max_chars is a character budget standing in for a token budget: roughly
    6k tokens of Italian prose, comfortably inside any provider's window while
    leaving room for the prompt itself.
    """
    original_bytes = len(html.encode("utf-8", errors="ignore"))

    tree = HTMLParser(html)
    for tag in NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()

    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""

    # Collapse runs of blank lines that survive decompose().
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    return CleanedPage(
        text=text,
        original_bytes=original_bytes,
        text_length=len(text),
        truncated=truncated,
    )

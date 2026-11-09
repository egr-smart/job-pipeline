import html
import re

from bs4 import BeautifulSoup


def html_to_text(raw: str | None, *, unescape: bool = False) -> str:
    """Convert an HTML job description to readable plain text."""
    if not raw:
        return ""
    if unescape:  # Greenhouse returns entity-escaped HTML
        raw = html.unescape(raw)
    soup = BeautifulSoup(raw, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def normalize_words(s: str) -> str:
    """Lowercase, turn separators into spaces and collapse whitespace."""
    s = s.lower().replace("–", " ").replace("—", " ")
    s = re.sub(r"[-_/|,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive phrase match. `text` should already be normalised."""
    return re.search(rf"(?<![a-z0-9]){re.escape(normalize_words(phrase))}(?![a-z0-9])", text) is not None

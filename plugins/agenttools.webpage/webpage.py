#!/usr/bin/env python3
import html
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urlparse

MAX_FETCH_BYTES = 1_000_000
MAX_EXTRACTED_BYTES = 12_000
MAX_FINAL_CONTEXT_BYTES = 10_000
MAX_CHUNK_BYTES = 2_000
MAX_CHUNK_PASSES = 6
CURL_MAX_TIME = "20"
DROP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}
BLOCK_TAGS = {
    "article", "aside", "blockquote", "br", "div", "dd", "dl", "dt", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "th", "thead", "tr", "ul"
}
SKIP_ATTR_PATTERNS = [
    re.compile(r"cookie", re.I),
    re.compile(r"consent", re.I),
    re.compile(r"subscribe", re.I),
    re.compile(r"newsletter", re.I),
    re.compile(r"footer", re.I),
    re.compile(r"header", re.I),
    re.compile(r"nav", re.I),
    re.compile(r"sidebar", re.I),
    re.compile(r"breadcrumb", re.I),
    re.compile(r"social", re.I),
    re.compile(r"share", re.I),
    re.compile(r"modal", re.I),
    re.compile(r"popup", re.I),
    re.compile(r"advert", re.I),
    re.compile(r"promo", re.I),
]
WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
BLANK_RUN_RE = re.compile(r"\n{3,}")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

FOCUS_HINT_WEIGHTS = {
    "release": 8,
    "releases": 8,
    "changelog": 8,
    "changes": 6,
    "changed": 6,
    "version": 6,
    "versions": 6,
    "ship": 5,
    "shipping": 5,
    "shipped": 5,
    "latest": 4,
    "momentum": 4,
}

SECTION_HINT_WEIGHTS = {
    "release": 8,
    "releases": 8,
    "release momentum": 10,
    "now shipping": 8,
    "what's new": 8,
    "latest": 4,
    "changelog": 8,
}


def error(message: str) -> None:
    raise RuntimeError(message)


def is_supported_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_input_text(text: str) -> Tuple[str, Optional[str]]:
    lines = [line.rstrip() for line in text.splitlines()]
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        error("missing URL. The first non-empty line must be an http or https URL.")
    url = meaningful[0].strip()
    if not is_supported_url(url):
        error(f"unsupported URL: {url}. Only http and https are allowed.")
    focus_lines = meaningful[1:]
    focus = "\n".join(focus_lines).strip() or None
    return url, focus


def clip_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    clipped = encoded[:limit]
    return clipped.decode("utf-8", errors="ignore")


def split_lines_by_utf8_budget(lines: List[str], limit: int) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append("\n".join(current).strip())
            current = []

    for line in lines:
        candidate = "\n".join(current + [line]).strip() if current else line
        if len(candidate.encode("utf-8")) <= limit:
            current.append(line)
            continue
        flush()
        if len(line.encode("utf-8")) <= limit:
            current.append(line)
            continue
        fragments = split_text_to_budget(line, limit)
        for fragment in fragments[:-1]:
            chunks.append(fragment)
        if fragments:
            current = [fragments[-1]]
    flush()
    return [chunk for chunk in chunks if chunk]


def split_text_to_budget(text: str, limit: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]
    pieces = SENTENCE_SPLIT_RE.split(text)
    if len(pieces) <= 1:
        encoded = text.encode("utf-8")
        output = []
        start = 0
        while start < len(encoded):
            output.append(encoded[start:start + limit].decode("utf-8", errors="ignore").strip())
            start += limit
        return [piece for piece in output if piece]
    return split_lines_by_utf8_budget([piece.strip() for piece in pieces if piece.strip()], limit)


def fetch_url(url: str) -> str:
    curl = subprocess.run(
        [
            "curl", "-L", "--fail", "--silent", "--show-error", "--compressed",
            "--max-time", CURL_MAX_TIME, url,
        ],
        capture_output=True,
        text=False,
        check=False,
    )
    if curl.returncode != 0:
        stderr = curl.stderr.decode("utf-8", errors="ignore").strip()
        error(stderr or f"curl failed with exit code {curl.returncode}")
    payload = curl.stdout[:MAX_FETCH_BYTES]
    return payload.decode("utf-8", errors="ignore")


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.in_title = False
        self.skip_depth = 0
        self.buffer: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_text = " ".join(value for _, value in attrs if value)
        if tag == "title":
            self.in_title = True
        if self.skip_depth > 0:
            if tag in DROP_TAGS:
                self.skip_depth += 1
            return
        if tag in DROP_TAGS or any(pattern.search(attr_text) for pattern in SKIP_ATTR_PATTERNS):
            self.skip_depth = 1
            return
        if tag in BLOCK_TAGS:
            self.buffer.append("\n")
        if tag == "li":
            self.buffer.append("- ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if self.skip_depth > 0:
            if tag in DROP_TAGS:
                self.skip_depth -= 1
            elif self.skip_depth == 1:
                self.skip_depth = 0
            return
        if tag in BLOCK_TAGS:
            self.buffer.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self.skip_depth > 0:
            return
        text = html.unescape(data)
        text = WHITESPACE_RE.sub(" ", text)
        if text.strip():
            self.buffer.append(text.strip())
            self.buffer.append(" ")

    def text(self) -> str:
        text = "".join(self.buffer)
        text = text.replace(" \n", "\n").replace("\n ", "\n")
        text = BLANK_RUN_RE.sub("\n\n", text)
        lines = []
        for raw in text.splitlines():
            cleaned = raw.strip()
            if cleaned:
                lines.append(cleaned)
            elif lines and lines[-1] != "":
                lines.append("")
        return "\n".join(lines).strip()


def extract_readable_content(source_url: str, html_text: str) -> Tuple[str, str]:
    parser = ReadableHTMLParser()
    parser.feed(html_text)
    title = WHITESPACE_RE.sub(" ", parser.title).strip() or source_url
    body = parser.text()
    if not body:
        error("unable to extract readable webpage text")
    return title, clip_utf8(body, MAX_EXTRACTED_BYTES)


def focus_terms(focus: Optional[str]) -> List[str]:
    if not focus:
        return []
    return re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", focus.lower())


def chunk_score(text: str, focus: Optional[str]) -> int:
    lowered = text.lower()
    score = 0
    for term in focus_terms(focus):
        if term in lowered:
            score += 5
    for token, weight in FOCUS_HINT_WEIGHTS.items():
        if token in lowered and focus and token in focus.lower():
            score += weight
    for token, weight in SECTION_HINT_WEIGHTS.items():
        if token in lowered:
            score += weight
    if re.search(r"\b\d+\.\d+(?:\.\d+)?\b", lowered):
        score += 4
    if "- " in text or "\n- " in text:
        score += 2
    return score


def build_semantic_chunks(cleaned_text: str, focus: Optional[str]) -> List[str]:
    sections: List[List[str]] = []
    current: List[str] = []
    for line in cleaned_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                sections.append(current)
                current = []
            continue
        if current and (stripped.endswith(":") or stripped.startswith("#") or len(stripped.split()) <= 6):
            sections.append(current)
            current = [stripped]
            continue
        current.append(stripped)
    if current:
        sections.append(current)

    candidate_chunks: List[str] = []
    for section in sections:
        candidate_chunks.extend(split_lines_by_utf8_budget(section, MAX_CHUNK_BYTES))

    ranked = sorted(
        ((index, text, chunk_score(text, focus)) for index, text in enumerate(candidate_chunks)),
        key=lambda item: (item[2], -item[0]),
        reverse=True,
    )
    positive = [text for _, text, score in ranked if score > 0]
    selected = positive[:MAX_CHUNK_PASSES]
    if not selected:
        selected = [text for _, text, _ in ranked[:MAX_CHUNK_PASSES]]
    if not selected:
        return split_text_to_budget(cleaned_text, MAX_CHUNK_BYTES)[:MAX_CHUNK_PASSES]
    return selected


def build_nested_prompt(url: str, title: str, focus: Optional[str], cleaned_text: str) -> str:
    parts = [
        "You are converting fetched webpage content into concise plain text for a very small local model context.",
        "Return plain text only.",
        "Do not include raw HTML, navigation, cookie banners, subscribe prompts, social share UI, repeated footer text, or image placeholders.",
        "Preserve the page title, the source URL, and the most important facts, headings, and short lists.",
        "If the page includes links that matter to the requested focus, mention them briefly in plain text.",
    ]
    if focus:
        parts.append(f"Prioritize this user focus: {focus}")
    parts.extend([
        f"Source URL: {url}",
        f"Extracted title: {title}",
        "Fetched readable text:",
        cleaned_text,
    ])
    return "\n\n".join(parts)


def build_chunk_prompt(url: str, title: str, focus: Optional[str], chunk_index: int, chunk_text: str) -> str:
    parts = [
        "You are reviewing one chunk of readable webpage text for later synthesis.",
        "Return plain text only.",
        "Be terse.",
        "Start with 'RELEVANCE: high|medium|low'.",
        "Then include at most 4 bullets with facts, release notes, version labels, or headings that matter.",
        "Ignore generic marketing copy, nav, install commands, and repeated chrome unless directly relevant.",
        f"Source URL: {url}",
        f"Page title: {title}",
        f"Chunk number: {chunk_index}",
    ]
    if focus:
        parts.append(f"User focus: {focus}")
    parts.extend(["Chunk text:", chunk_text])
    return "\n\n".join(parts)


def build_final_prompt(url: str, title: str, focus: Optional[str], chunk_summaries: List[str]) -> str:
    combined = "\n\n".join(
        f"Chunk summary {index + 1}:\n{summary.strip()}" for index, summary in enumerate(chunk_summaries) if summary.strip()
    )
    parts = [
        "You are combining webpage chunk notes into one concise plain-text answer.",
        "Return plain text only.",
        "Preserve the source URL and page title.",
        "Prefer concrete release changes, versions, and new capabilities over general positioning copy.",
        "If the focus asks about release changes, center the answer on what shipped and version progression.",
        f"Source URL: {url}",
        f"Page title: {title}",
    ]
    if focus:
        parts.append(f"User focus: {focus}")
    parts.extend(["Chunk summaries:", clip_utf8(combined, MAX_FINAL_CONTEXT_BYTES)])
    return "\n\n".join(parts)


def run_nested_agent(prompt: str) -> str:
    omux_cli = os.environ.get("OMUX_CLI", "omux")
    result = subprocess.run(
        [omux_cli, "agent", "-p", prompt],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        error(stderr or stdout or f"omux agent failed with exit code {result.returncode}")
    return result.stdout.strip()


def fallback_output(url: str, title: str, focus: Optional[str], cleaned_text: str, reason: str) -> str:
    chunks = build_semantic_chunks(cleaned_text, focus)
    focused_excerpt = "\n\n".join(chunks)
    parts = [
        f"TITLE: {title}",
        f"SOURCE: {url}",
        f"NOTE: Nested omux agent summarization unavailable. {reason}",
    ]
    if focus:
        parts.append(f"FOCUS: {focus}")
    parts.extend(["CONTENT:", focused_excerpt])
    return "\n".join(parts)


def summarize_url_input(input_text: str) -> str:
    url, focus = parse_input_text(input_text)
    html_text = fetch_url(url)
    title, cleaned_text = extract_readable_content(url, html_text)
    chunks = build_semantic_chunks(cleaned_text, focus)
    try:
        if len(chunks) <= 1:
            prompt = build_nested_prompt(url, title, focus, cleaned_text)
            return run_nested_agent(prompt)
        chunk_summaries = [
            run_nested_agent(build_chunk_prompt(url, title, focus, index + 1, chunk))
            for index, chunk in enumerate(chunks)
        ]
        return run_nested_agent(build_final_prompt(url, title, focus, chunk_summaries))
    except RuntimeError as exc:
        return fallback_output(url, title, focus, cleaned_text, str(exc))


def callback_mode() -> int:
    request = json.load(sys.stdin)
    input_text = request.get("input", "")
    try:
        output = summarize_url_input(input_text)
        json.dump({"ok": True, "output": output}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except RuntimeError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 0


def cli_mode(argv: List[str]) -> int:
    if not argv:
        print("agenttools.webpage: missing URL", file=sys.stderr)
        return 1
    url = argv[0]
    focus = " ".join(argv[1:]).strip()
    input_text = url if not focus else f"{url}\n{focus}"
    try:
        print(summarize_url_input(input_text))
        return 0
    except RuntimeError as exc:
        print(f"agenttools.webpage: {exc}", file=sys.stderr)
        return 1


def main(argv: List[str]) -> int:
    if not argv:
        print("usage: webpage.py callback|cli [...args]", file=sys.stderr)
        return 1
    mode = argv[0]
    if mode == "callback":
        return callback_mode()
    if mode == "cli":
        return cli_mode(argv[1:])
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

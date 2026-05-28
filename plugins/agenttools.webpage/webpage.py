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
    parts = [
        f"TITLE: {title}",
        f"SOURCE: {url}",
        f"NOTE: Nested omux agent summarization unavailable. {reason}",
    ]
    if focus:
        parts.append(f"FOCUS: {focus}")
    parts.extend(["CONTENT:", cleaned_text])
    return "\n".join(parts)


def summarize_url_input(input_text: str) -> str:
    url, focus = parse_input_text(input_text)
    html_text = fetch_url(url)
    title, cleaned_text = extract_readable_content(url, html_text)
    prompt = build_nested_prompt(url, title, focus, cleaned_text)
    try:
        return run_nested_agent(prompt)
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

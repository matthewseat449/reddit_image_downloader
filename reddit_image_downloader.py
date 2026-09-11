#!/usr/bin/env python3
"""Download direct images from a subreddit's Top / All Time web pages.

This version does not use Reddit's API and does not require API credentials.
It reads old.reddit.com listing pages instead. Reddit may require a browser
session cookie; set REDDIT_COOKIE to a Cookie header copied from that session.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) subreddit-image-downloader/1.0"
WEB_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    # Old Reddit uses this preference cookie to show mature communities.
    "Cookie": "over18=1",
}
REDDIT_COOKIE_ENV = "REDDIT_COOKIE"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
EXTENSION_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
REQUEST_DELAY = 2.0
MIN_REQUEST_DELAY = 5.0
MAX_RATE_LIMIT_RETRIES = 4
_LAST_REQUEST_AT: float | None = None


def wait_for_request_slot() -> None:
    """Ensure every outbound request is separated by REQUEST_DELAY seconds."""
    global _LAST_REQUEST_AT
    if _LAST_REQUEST_AT is not None:
        remaining = REQUEST_DELAY - (time.monotonic() - _LAST_REQUEST_AT)
        if remaining > 0:
            time.sleep(remaining)
    _LAST_REQUEST_AT = time.monotonic()


def open_with_rate_limit_backoff(request: Request, timeout: int):
    """Open a URL, spacing requests and retrying HTTP 429 responses."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        wait_for_request_slot()
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as error:
            if error.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            retry_after = error.headers.get("Retry-After", "")
            try:
                server_delay = float(retry_after)
            except ValueError:
                server_delay = 0.0
            backoff = max(REQUEST_DELAY * (2 ** (attempt + 1)), server_delay, 10.0)
            print(
                f"Rate limited by server; waiting {backoff:g} seconds before retry "
                f"{attempt + 1}/{MAX_RATE_LIMIT_RETRIES}...",
                file=sys.stderr,
            )
            time.sleep(backoff)


class ListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[tuple[str, str, str, str]] = []
        self.next_page: str | None = None
        self._post_id = "unknown"
        self._score = "0"
        self._permalink = ""

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())

        if tag == "div" and "thing" in classes:
            self._post_id = (attrs.get("data-fullname") or "unknown").removeprefix("t3_")
            self._score = attrs.get("data-score") or "0"
            self._permalink = attrs.get("data-permalink") or ""
        elif tag == "a" and "title" in classes and attrs.get("href"):
            self.posts.append(
                (self._post_id, self._score, html.unescape(attrs["href"]), self._permalink)
            )
        elif tag == "span" and "next-button" in classes:
            # The actual link is the next <a> tag.
            self._expect_next_link = True
        elif tag == "a" and getattr(self, "_expect_next_link", False):
            self.next_page = html.unescape(attrs.get("href") or "") or None
            self._expect_next_link = False


class ImageMetadataParser(HTMLParser):
    """Collect full-size images advertised by a linked web page."""

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attrs = {key.lower(): value for key, value in attrs_list}
        image_kind = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if content and image_kind in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            self.images.append(urljoin(self.page_url, html.unescape(content)))


def get_page(url: str) -> str:
    headers = dict(WEB_HEADERS)
    if (urlparse(url).hostname or "").lower().endswith("reddit.com"):
        cookie = os.environ.get(REDDIT_COOKIE_ENV)
        if cookie:
            headers["Cookie"] = cookie
    else:
        headers.pop("Cookie", None)
    request = Request(url, headers=headers)
    with open_with_rate_limit_backoff(request, timeout=30) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def explain_empty_listing(page: str) -> str:
    lower = page.lower()
    if "welcome to reddit" in lower or "reason=lor2" in lower:
        return (
            "Reddit redirected the anonymous request to login. Set REDDIT_COOKIE "
            "to a Cookie header from an active browser session and try again."
        )
    if "you must be 18+" in lower or "over18" in lower and "interstitial" in lower:
        return "Reddit returned an age-verification page instead of the listing."
    if "this community is private" in lower or "private subreddit" in lower:
        return "This community is private; anonymous page access cannot read it."
    if "has been banned" in lower or "this subreddit was banned" in lower:
        return "This community has been banned."
    if "page not found" in lower or "there doesn't seem to be anything here" in lower:
        return "The subreddit was not found, has no visible top posts, or Reddit hid the listing."
    if "blocked" in lower or "whoa there" in lower or "request has been blocked" in lower:
        return "Reddit blocked the anonymous page request. Try again later or from a browser session."
    return "Reddit returned a page with no recognizable posts; its page layout or access rules may have changed."


def unwrap_reddit_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("reddit.com") and parsed.path == "/out":
        return parse_qs(parsed.query).get("url", [url])[0]
    return url


def direct_image_url(url: str) -> str | None:
    url = unwrap_reddit_redirect(urljoin("https://old.reddit.com", url))
    parsed = urlparse(url)
    if Path(parsed.path).suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    # preview.redd.it commonly serves a width-limited or WebP-converted copy.
    # The same path on i.redd.it is the original uploaded image. Query strings
    # on i.redd.it are not needed and may contain resizing/conversion options.
    hostname = (parsed.hostname or "").lower()
    if hostname == "preview.redd.it":
        parsed = parsed._replace(netloc="i.redd.it", query="", fragment="")
    elif hostname == "i.redd.it":
        parsed = parsed._replace(query="", fragment="")
    return urlunparse(parsed)


def canonical_image_url(url: str) -> str | None:
    """Return the best-resolution form of a discovered image URL."""
    url = html.unescape(url).replace("\\u0026", "&").replace("\\/", "/")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == "preview.redd.it":
        parsed = parsed._replace(netloc="i.redd.it", query="", fragment="")
    elif hostname == "i.redd.it":
        parsed = parsed._replace(query="", fragment="")
    url = urlunparse(parsed)
    return direct_image_url(url)


def images_from_post_page(page_url: str) -> list[str]:
    """Find Reddit gallery originals and images advertised by external pages."""
    page = get_page(page_url)
    parser = ImageMetadataParser(page_url)
    parser.feed(page)

    # Reddit embeds every gallery item's source URL in its page data. This also
    # works when only the first gallery image is present in og:image metadata.
    decoded_page = html.unescape(page).replace("\\u0026", "&").replace("\\/", "/")
    reddit_images = re.findall(
        r"https?://(?:i|preview)\.redd\.it/[^\"'<>\s]+",
        decoded_page,
        flags=re.IGNORECASE,
    )
    parser.images.extend(reddit_images)

    results: list[str] = []
    seen: set[str] = set()
    for url in parser.images:
        best = canonical_image_url(url)
        if best and best not in seen:
            seen.add(best)
            results.append(best)
    return results


def post_image_urls(link: str, permalink: str) -> list[str]:
    direct = direct_image_url(link)
    if direct:
        return [direct]

    absolute_link = urljoin("https://old.reddit.com", unwrap_reddit_redirect(link))
    parsed = urlparse(absolute_link)
    is_reddit_post = parsed.hostname in {"reddit.com", "www.reddit.com", "old.reddit.com"}
    page_url = urljoin("https://old.reddit.com", permalink) if is_reddit_post and permalink else absolute_link
    return images_from_post_page(page_url)


def safe_fragment(value: str, limit: int = 80) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return value[:limit] or "untitled"


def download_image(url: str, destination_stem: Path) -> tuple[Path, bool]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://old.reddit.com/",
            # Avoid asking CDNs to convert an original JPEG/PNG into WebP.
            "Accept": "image/png,image/jpeg,image/gif,image/webp,image/*;q=0.8,*/*;q=0.5",
        },
    )
    with open_with_rate_limit_backoff(request, timeout=60) as response:
        content_type = response.headers.get_content_type().lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"server returned {content_type}, not an image")
        extension = Path(urlparse(url).path).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            extension = EXTENSION_BY_TYPE.get(content_type) or mimetypes.guess_extension(content_type) or ".img"
        destination = destination_stem.with_suffix(extension)
        if destination.exists():
            return destination, False
        temporary = destination.with_suffix(destination.suffix + ".part")
        with temporary.open("wb") as output:
            while chunk := response.read(1024 * 256):
                output.write(chunk)
        temporary.replace(destination)
        return destination, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subreddit", nargs="?", help="Subreddit name, with or without r/")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("."),
        help="Parent output directory (default: current directory)",
    )
    parser.add_argument("--max-posts", type=int, help="Stop after checking this many top posts")
    parser.add_argument(
        "--delay",
        type=float,
        default=MIN_REQUEST_DELAY,
        help=f"Minimum seconds between every web request (minimum/default: {MIN_REQUEST_DELAY:g})",
    )
    return parser.parse_args()


def main() -> int:
    global REQUEST_DELAY
    args = parse_args()
    subreddit = (args.subreddit or input("Subreddit: ")).strip().removeprefix("r/").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
        print("Invalid subreddit name.", file=sys.stderr)
        return 2
    if args.max_posts is not None and args.max_posts < 1:
        print("--max-posts must be at least 1.", file=sys.stderr)
        return 2
    if args.delay < MIN_REQUEST_DELAY:
        print(
            f"Requested --delay {args.delay:g} is too short; using the enforced minimum "
            f"of {MIN_REQUEST_DELAY:g} seconds.",
            file=sys.stderr,
        )
        args.delay = MIN_REQUEST_DELAY
    REQUEST_DELAY = args.delay

    output_dir = args.output / safe_fragment(subreddit)
    output_dir.mkdir(parents=True, exist_ok=True)
    query = urlencode({"sort": "top", "t": "all", "limit": 100})
    page_url: str | None = f"https://old.reddit.com/r/{subreddit}/top/?{query}"
    checked = downloaded = existing = skipped = failed = 0
    seen_posts: set[str] = set()

    try:
        while page_url and (args.max_posts is None or checked < args.max_posts):
            parser = ListingParser()
            page = get_page(page_url)
            parser.feed(page)
            if not parser.posts and checked == 0:
                print(f"Could not read posts: {explain_empty_listing(page)}", file=sys.stderr)
                return 1
            new_posts = 0
            for post_id, score, link, permalink in parser.posts:
                if post_id in seen_posts:
                    continue
                seen_posts.add(post_id)
                new_posts += 1
                checked += 1
                try:
                    image_urls = post_image_urls(link, permalink)
                except (HTTPError, URLError, OSError, ValueError) as error:
                    failed += 1
                    print(f"[{checked}] Could not inspect post page: {error}", file=sys.stderr)
                    image_urls = []
                if not image_urls:
                    skipped += 1
                else:
                    for image_number, image_url in enumerate(image_urls, start=1):
                        stem = output_dir / (
                            f"{checked:04d}_{safe_fragment(score)}_{safe_fragment(post_id)}_{image_number:02d}"
                        )
                        try:
                            path, was_downloaded = download_image(image_url, stem)
                            downloaded += int(was_downloaded)
                            existing += int(not was_downloaded)
                            print(f"[{checked}.{image_number}] {path}")
                        except (HTTPError, URLError, OSError, ValueError) as error:
                            failed += 1
                            print(f"[{checked}.{image_number}] Could not download {image_url}: {error}", file=sys.stderr)
                if args.max_posts is not None and checked >= args.max_posts:
                    break

            if not new_posts or not parser.next_page:
                break
            page_url = parser.next_page
    except HTTPError as error:
        if error.code in {403, 429}:
            print(
                f"Reddit blocked or rate-limited the page request (HTTP {error.code}). "
                "Set REDDIT_COOKIE from an active browser session and try again, or wait and retry later.",
                file=sys.stderr,
            )
        else:
            print(f"Reddit returned HTTP {error.code}: {error.reason}", file=sys.stderr)
        return 1
    except (URLError, OSError) as error:
        print(f"Network error: {error}", file=sys.stderr)
        return 1

    print(
        f"Done: checked {checked} post(s), downloaded {downloaded}, already present {existing}, "
        f"skipped {skipped} non-direct-image post(s), failed {failed}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

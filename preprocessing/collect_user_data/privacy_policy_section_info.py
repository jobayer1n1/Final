#!/usr/bin/env python3
"""
privacy_policy_section_info.py

Checks Chrome Web Store extension pages for privacy policy information.

For each extension ID, it fetches the Chrome Web Store detail page,
locates the Privacy section, and determines:
- The URL of the privacy policy, if any (policy_link: string, null if no
  link was found on an otherwise-readable page, or "unknown" if the page
  itself couldn't be confidently read)
- Whether the developer collects user data
  (collect_user_data: "no" = does NOT collect, "yes" = collects / mentions
   collection, "unknown" = could not determine)

Input extension IDs can come from any combination of:
--unzipped-dir : directory containing unpacked extensions (each subfolder is an extension ID)
--zipped-dir   : directory containing .crx files named {ext_id}.crx
--list         : text file with one extension ID per line
--id           : a single extension ID, for a quick one-off test
--limit        : optional cap on how many of the loaded extension IDs to process

Outputs result.json and result.csv in the --out directory.

Error handling philosophy
-------------------------
To minimise false positives, any uncertainty in detection causes the affected
field(s) to be set to "unknown" and the reason recorded in the `errors` field.
Only when the parser is confident does it emit "no"/"yes" or an actual URL.

Special case: "Item not available"
----------------------------------
Some extensions are not (or no longer) available on the Chrome Web Store.
When that happens, Chrome Web Store redirects the request to the same
/detail/{slug}/{ext_id} URL shape but with the literal placeholder slug
"empty-title" (there's no title to build a real slug from). Because that
page has no Privacy section, the naive parser would report
`privacy_section_missing`. Instead, we detect this condition primarily by
checking the *final* URL after redirects for that "empty-title" slug, with
page-content checks (canonical/og:url tags, the "This item is not
available" heading) as a fallback in case the redirect happens purely via
client-side JavaScript routing that a plain HTTP client can't see. We
record it as its own error code, `item_not_available`, which is far more
actionable.

Compatibility
-------------
Uses the modern `X | None` union syntax; `from __future__ import annotations`
makes this work on Python 3.7+ without evaluating the annotations at runtime.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Please install: pip install requests beautifulsoup4")
    sys.exit(1)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
BASE_URL = "https://chromewebstore.google.com/detail/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Exact literal strings Chrome Web Store emits in the Privacy section.
# Confirmed by inspecting live extension pages (Sept 2026):
#   - No collection: "The developer has disclosed that it will not collect
#     or use your data."
#   - Does collect:  "{ExtensionName} has disclosed the following information
#     regarding the collection and usage of your data." — the extension name
#     varies, so we match on the stable suffix only.
# We compare against these literals exactly (substring containment of the
# full, fixed wording) rather than loose keyword heuristics, since a single
# generic word like "data" or "collect" is present in almost any privacy
# text and produced false positives.
NO_COLLECTION_PHRASE = "The developer has disclosed that it will not collect or use your data."
COLLECTS_DATA_PHRASE = "has disclosed the following information regarding the collection and usage of your data."

# Heuristic indicators that the page is a stub / captcha / rate-limit response
BLOCK_INDICATORS = [
    "our systems have detected unusual traffic",
    "unusual traffic from your computer",
    "this browser or app may not be secure",
    "enable javascript and cookies to continue",
    "please enable cookies and reload the page",
    "too many requests",
    "error 429",
]

# Chrome Web Store detail URLs are shaped /detail/{slug}/{ext_id}, where the
# slug is derived from the extension's title. When an extension is removed,
# unpublished, or never existed, there's no title to slug — Google redirects
# to the same path but with the literal placeholder slug "empty-title", e.g.
#   https://chromewebstore.google.com/detail/empty-title/{ext_id}
# (Confirmed against a real redirect target; an earlier assumption that this
# used an "/error" suffix instead was wrong.) We detect this via the final
# URL after redirects rather than by matching page text, since URL shape is
# more stable than markup/copy.
ITEM_NOT_AVAILABLE_URL_RE = re.compile(r"/detail/empty-title/", re.IGNORECASE)

# Sanity threshold: a real CWS page is large; a stub is not
MIN_PAGE_SIZE = 5000

# Sentinel value used for "could not determine" in the policy_link field.
UNKNOWN = "unknown"

# Thread-local storage for per-thread requests sessions
_thread_local = threading.local()


# ----------------------------------------------------------------------
# Console output formatting
# ----------------------------------------------------------------------
class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def supports_color(no_color_flag: bool) -> bool:
    """Decide whether to emit ANSI colors: respects --no-color, the NO_COLOR
    and FORCE_COLOR conventions, and falls back to auto-detecting a TTY so
    piping output to a file or log doesn't fill it with escape codes."""
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return sys.stdout.isatty()


def _c(text: str, code: str, enabled: bool) -> str:
    """Wrap text in an ANSI color code, or return it unchanged if disabled."""
    if not enabled or not text:
        return text
    return f"{code}{text}{_Ansi.RESET}"


def truncate(text: str, max_len: int = 65) -> str:
    """Shorten long strings (e.g. policy links) so a line never wraps."""
    if text is None or len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def format_duration(seconds: float) -> str:
    """Render a duration like '2m 34s' or '1h 05m 03s'."""
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_collect_flag(value, color: bool) -> str:
    """Colorize the collect_user_data label for terminal display."""
    code = {"no": _Ansi.GREEN, "yes": _Ansi.YELLOW, "unknown": _Ansi.GRAY}.get(
        value, _Ansi.GRAY
    )
    return _c(str(value), code, color)


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def get_session() -> requests.Session:
    """Return a thread-local requests.Session."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def _is_valid_ext_id(ext_id: str) -> bool:
    """Chrome extension IDs are 32 lowercase letters a-p."""
    return bool(re.fullmatch(r"[a-p]{32}", ext_id))


def get_extension_ids_from_unzipped_dir(unzipped_dir: str) -> list:
    ids = []
    if not os.path.isdir(unzipped_dir):
        print(f"Warning: --unzipped-dir '{unzipped_dir}' does not exist.")
        return ids
    for entry in os.listdir(unzipped_dir):
        full_path = os.path.join(unzipped_dir, entry)
        if os.path.isdir(full_path):
            if _is_valid_ext_id(entry):
                ids.append(entry)
            else:
                print(f"Warning: Skipping non-ID directory in --unzipped-dir: {entry}")
    return ids


def get_extension_ids_from_zipped_dir(zipped_dir: str) -> list:
    ids = []
    if not os.path.isdir(zipped_dir):
        print(f"Warning: --zipped-dir '{zipped_dir}' does not exist.")
        return ids
    for entry in os.listdir(zipped_dir):
        full_path = os.path.join(zipped_dir, entry)
        if not os.path.isfile(full_path):
            continue
        if not entry.lower().endswith(".crx"):
            print(f"Warning: Skipping non-.crx file in --zipped-dir: {entry}")
            continue
        ext_id = entry[: -len(".crx")]
        if _is_valid_ext_id(ext_id):
            ids.append(ext_id)
        else:
            print(f"Warning: Skipping invalid extension ID filename: {entry}")
    return ids


def get_extension_ids_from_list(list_file: str) -> list:
    ids = []
    if not os.path.isfile(list_file):
        print(f"Warning: --list file '{list_file}' does not exist.")
        return ids
    with open(list_file, "r", encoding="utf-8") as f:
        for line in f:
            ext_id = line.strip()
            if ext_id and _is_valid_ext_id(ext_id):
                ids.append(ext_id)
            elif ext_id:
                print(f"Warning: Skipping invalid ID in list: {ext_id}")
    return ids


def fetch_page(ext_id: str) -> tuple[str | None, str | None, list]:
    """
    Fetch the CWS detail page for ext_id.
    Returns (html, final_url, errors). html/final_url are None on failure.

    final_url is the URL after following redirects, which is what we use to
    detect the "item not available" redirect (a slug of "empty-title" in
    .../detail/empty-title/{ext_id}).
    """
    session = get_session()
    url = BASE_URL.format(ext_id)
    try:
        resp = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp.text, resp.url, []
    except requests.RequestException as e:
        print(f"Error fetching {ext_id}: {e}")
        return None, None, ["fetch_failed"]


# ----------------------------------------------------------------------
# Page-level sanity checks
# ----------------------------------------------------------------------
def is_item_not_available_url(url: str | None) -> bool:
    """True if the given URL points at CWS's 'item not available' page,
    e.g. https://chromewebstore.google.com/detail/empty-title/{ext_id}"""
    if not url:
        return False
    return bool(ITEM_NOT_AVAILABLE_URL_RE.search(url))


def is_item_not_available(html: str | None, final_url: str | None) -> bool:
    """
    Detect Chrome Web Store's 'Item not available' page/redirect.

    CWS builds detail URLs as /detail/{slug}/{ext_id}. For a removed,
    unpublished, or never-existing extension, the browser ends up at
    .../detail/empty-title/{ext_id} (no title to slug from), but that move
    may happen via client-side JavaScript routing rather than a real HTTP
    3xx redirect — so a plain `requests` fetch (which doesn't execute JS)
    can land on a 200 response at the *original* URL whose server-rendered
    content already reflects the "not available" state. To catch this
    reliably regardless of which mechanism CWS uses at any given time, we
    check three independent signals and flag it if any one of them fires:

      1. A real HTTP redirect already landed on the "empty-title" slug
         (resp.url, i.e. final_url, contains "/detail/empty-title/").
      2. The server-rendered HTML embeds that same "empty-title" URL in a
         <link rel="canonical"> or <meta property="og:url"> tag, even
         though the visible address bar hasn't been updated yet by the
         client-side router.
      3. The page renders the literal "This item is not available" heading
         text server-side (the most direct signal, and the one actually
         seen by a user's browser).
    """
    if is_item_not_available_url(final_url):
        return True

    if not html:
        return False

    # Signal 2: canonical / og:url meta tags pointing at the "empty-title"
    # slug. Cheap regex scan on raw HTML is enough here; a full soup parse
    # isn't needed just to pull an href/content attribute out of <head>.
    for match in re.finditer(
        r'<(?:link|meta)\b[^>]*?(?:href|content)=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        if is_item_not_available_url(match.group(1)):
            return True

    # Signal 3: the literal, user-visible heading text, rendered server-side.
    if "this item is not available" in html.lower():
        return True

    return False


def check_page_sanity(html: str | None, final_url: str | None, ext_id: str) -> list:
    """
    Perform cheap checks to detect pages that clearly aren't the real
    extension detail page (stubs, captchas, rate limits, 404s, or the
    'Item not available' page).

    Returns a list of error codes (empty if the page looks fine).
    """
    errors = []

    # "Item not available" is a valid response from Google, not a stub.
    # Check this first so it takes precedence over generic size/content
    # heuristics that wouldn't apply to the error page.
    if is_item_not_available(html, final_url):
        errors.append("item_not_available")
        return errors

    if html is None or not html.strip():
        errors.append("empty_response")
        return errors

    if len(html) < MIN_PAGE_SIZE:
        errors.append("page_too_short")

    lowered = html.lower()
    for indicator in BLOCK_INDICATORS:
        if indicator in lowered:
            errors.append("blocked_or_stub_page")
            break

    # The extension ID should appear in the HTML (canonical URL, metadata, etc.)
    if ext_id not in html:
        errors.append("ext_id_not_in_page")

    return errors


# ----------------------------------------------------------------------
# Privacy section parsing
# ----------------------------------------------------------------------
def parse_privacy_section(html: str) -> tuple:
    """
    Locate the Privacy section and extract privacy data.

    Returns (policy_link, collect_user_data, errors):

      policy_link       : URL string (confident), None (section present but
                          no link found), or "unknown" (could not determine)
      collect_user_data : "no" (declares no collection), "yes" (collects /
                          mentions collection), or "unknown" (could not
                          determine)
      errors            : list of error codes (empty if none)

    NOTE: In the normal flow this function is only called when
    `check_page_sanity()` returned no errors (which already rules out the
    "item not available" redirect), so it assumes it's looking at a real
    extension detail page.
    """
    errors = []
    soup = BeautifulSoup(html, "html.parser")

    # --- Locate the Privacy section by <section><h2>Privacy</h2> ---
    privacy_section = None

    for section in soup.find_all("section"):
        h2 = section.find("h2")
        if h2 and h2.get_text(strip=True).lower() == "privacy":
            privacy_section = section
            break

    if privacy_section is None:
        # Fallback: did we at least see a Privacy heading anywhere?
        privacy_heading_elsewhere = False
        for h in soup.find_all(["h1", "h2", "h3", "h4"]):
            if h.get_text(strip=True).lower() == "privacy":
                privacy_heading_elsewhere = True
                break

        errors.append("privacy_section_missing")
        if privacy_heading_elsewhere:
            errors.append("privacy_heading_without_section")
        return UNKNOWN, "unknown", errors

    section_text = privacy_section.get_text(" ", strip=True)

    if not section_text or len(section_text) < 20:
        errors.append("privacy_section_empty")
        return UNKNOWN, "unknown", errors

    # --- Determine data collection status ---
    # Compare against CWS's exact, known literal strings only. If neither
    # literal is present, we can't be confident of the status, so we report
    # "unknown" rather than guess from loose keywords (see constants above).
    collect_user_data = "unknown"
    if NO_COLLECTION_PHRASE in section_text:
        collect_user_data = "no"
    elif COLLECTS_DATA_PHRASE in section_text:
        collect_user_data = "yes"
    else:
        errors.append("collection_status_unknown")

    # --- Find the privacy policy link ---
    policy_link = None
    for a in privacy_section.find_all("a", href=True):
        link_text = a.get_text(" ", strip=True).lower()
        href = (a["href"] or "").strip()

        if not href or href == "#" or href.lower().startswith("javascript:"):
            continue

        looks_like_policy = (
            "privacy policy" in link_text
            or "privacy-policy" in href.lower()
            or "/privacy" in href.lower()
        )
        if looks_like_policy:
            policy_link = href
            break

    return policy_link, collect_user_data, errors


# ----------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------
def process_extension(ext_id: str, delay: float) -> tuple:
    """Fetch and parse a single extension page."""
    html, final_url, fetch_errors = fetch_page(ext_id)

    if html is None:
        result = {
            "policy_link": UNKNOWN,
            "collect_user_data": "unknown",
            "errors": fetch_errors or ["fetch_failed"],
        }
    else:
        sanity_errors = check_page_sanity(html, final_url, ext_id)
        if sanity_errors:
            result = {
                "policy_link": UNKNOWN,
                "collect_user_data": "unknown",
                "errors": sanity_errors,
            }
        else:
            policy_link, collect_user_data, errors = parse_privacy_section(html)
            result = {
                "policy_link": policy_link,
                "collect_user_data": collect_user_data,
                "errors": errors if errors else None,
            }

    if delay > 0:
        time.sleep(delay)

    return ext_id, result


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Check Chrome Web Store extensions for privacy policy info."
    )
    parser.add_argument(
        "--unzipped-dir",
        dest="unzipped_dir",
        help="Directory containing unpacked extensions (subfolders are extension IDs)",
        default=None,
    )
    parser.add_argument(
        "--zipped-dir",
        dest="zipped_dir",
        help="Directory containing .crx files named {ext_id}.crx",
        default=None,
    )
    parser.add_argument(
        "--list",
        help="Text file with extension IDs, one per line",
        default=None,
    )
    parser.add_argument(
        "--id",
        dest="single_id",
        help="A single extension ID to check (handy for quick one-off tests).",
        default=None,
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for result.json and result.csv",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent workers (default: 1)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between requests per worker (default: 1.0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Only process this many extension IDs from the loaded/sorted list "
            "(useful for smoke-testing before a full run). Default: no limit."
        ),
    )
    parser.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        help="Disable colored terminal output (also respects the NO_COLOR env var).",
    )

    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    if not args.unzipped_dir and not args.zipped_dir and not args.list and not args.single_id:
        parser.error(
            "At least one of --unzipped-dir, --zipped-dir, --list, or --id must be provided."
        )

    ext_ids = set()
    if args.unzipped_dir:
        ext_ids.update(get_extension_ids_from_unzipped_dir(args.unzipped_dir))
    if args.zipped_dir:
        ext_ids.update(get_extension_ids_from_zipped_dir(args.zipped_dir))
    if args.list:
        ext_ids.update(get_extension_ids_from_list(args.list))
    if args.single_id:
        if _is_valid_ext_id(args.single_id):
            ext_ids.add(args.single_id)
        else:
            parser.error(f"--id '{args.single_id}' is not a valid extension ID (32 letters a-p).")

    if not ext_ids:
        print("No valid extension IDs found. Exiting.")
        sys.exit(1)

    sorted_ids = sorted(ext_ids)
    loaded_total = len(sorted_ids)

    if args.limit is not None and args.limit < loaded_total:
        sorted_ids = sorted_ids[: args.limit]

    total = len(sorted_ids)
    if args.limit is not None:
        print(
            f"Found {loaded_total} unique extension IDs; "
            f"processing {total} of them (--limit {args.limit}) "
            f"with {args.workers} worker(s)."
        )
    else:
        print(f"Found {total} unique extension IDs to process with {args.workers} worker(s).")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    use_color = supports_color(args.no_color)
    id_width = len(str(total))  # so the "completed/total" counter stays aligned

    results = {}
    error_counts = Counter()          # error code -> count
    collect_counts = Counter()        # collect_user_data value -> count
    policy_link_found = 0
    print_lock = threading.Lock()
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_extension, ext_id, args.delay): ext_id
            for ext_id in sorted_ids
        }

        completed = 0
        for future in as_completed(futures):
            completed += 1
            ext_id, result = future.result()
            results[ext_id] = result

            errors = result.get("errors")
            pct = (completed / total) * 100
            counter_display = f"{completed:>{id_width}}/{total} {pct:5.1f}%"

            with print_lock:
                if errors:
                    error_counts.update(errors)
                    status = _c("ERROR", _Ansi.RED + _Ansi.BOLD, use_color)
                    print(
                        f"[{counter_display}] {status} {ext_id}  "
                        f"{_c('; '.join(errors), _Ansi.DIM, use_color)}"
                    )
                else:
                    collect_counts.update([result["collect_user_data"]])
                    if result["policy_link"] not in (None, UNKNOWN):
                        policy_link_found += 1
                    status = _c("OK   ", _Ansi.GREEN + _Ansi.BOLD, use_color)
                    collect_display = format_collect_flag(
                        result["collect_user_data"], use_color
                    )
                    link_display = truncate(result["policy_link"] or "-")
                    print(
                        f"[{counter_display}] {status} {ext_id}  "
                        f"collects={collect_display}  "
                        f"{_c('policy=', _Ansi.GRAY, use_color)}{link_display}"
                    )

    elapsed = time.monotonic() - start_time

    ordered_results = {
        ext_id: results[ext_id] for ext_id in sorted_ids if ext_id in results
    }

    # ---------------- Summary ----------------
    # error_counts tallies individual error *codes* (an item can carry more
    # than one), so derive the OK/error item counts from the results directly.
    error_item_count = sum(1 for r in results.values() if r.get("errors"))
    ok_count = total - error_item_count

    rule = "=" * 60
    print()
    print(_c(rule, _Ansi.CYAN, use_color))
    print(
        f"Done in {format_duration(elapsed)} — {total} extension"
        f"{'s' if total != 1 else ''} processed"
    )
    ok_pct = (ok_count / total * 100) if total else 0.0
    err_pct = (error_item_count / total * 100) if total else 0.0
    print(f"  {_c('OK', _Ansi.GREEN, use_color):<12} {ok_count:>5}  ({ok_pct:5.1f}%)")
    print(f"  {_c('Errors', _Ansi.RED, use_color):<12} {error_item_count:>5}  ({err_pct:5.1f}%)")
    if error_counts:
        for code, count in error_counts.most_common():
            print(f"      {code:<28} {count:>5}")
    if ok_count:
        print()
        print(
            f"  Policy link found : {policy_link_found:>5}  "
            f"({policy_link_found / ok_count * 100:5.1f}% of OK)"
        )
        print(f"  Collects data      : {collect_counts.get('yes', 0):>5}")
        print(f"  Does not collect   : {collect_counts.get('no', 0):>5}")
        print(f"  Unknown            : {collect_counts.get('unknown', 0):>5}")
    print(_c(rule, _Ansi.CYAN, use_color))

    # ---------------- JSON ----------------
    json_path = out_dir / "privacy_policy_section.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(ordered_results, f, indent=2)
    print(f"JSON written to {json_path}")

    # ---------------- CSV ----------------
    csv_path = out_dir / "privacy_policy_section.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ext_id", "policy_link", "collect_user_data", "errors"])
        for ext_id, data in ordered_results.items():
            policy_link = data.get("policy_link")
            if policy_link is None:
                policy_link_cell = ""
            else:
                policy_link_cell = str(policy_link)

            cud_cell = data.get("collect_user_data") or ""

            errors = data.get("errors") or []
            errors_cell = "; ".join(errors)

            writer.writerow([ext_id, policy_link_cell, cud_cell, errors_cell])
    print(f"CSV written to {csv_path}")


if __name__ == "__main__":
    main()
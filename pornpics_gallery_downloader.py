#!/usr/bin/env python3
"""
Created by Steven Motie
pornpics_gallery_downloader.py

Downloads every hi-res image from a PornPics gallery page.
Stdlib only (no pip installs). Works on macOS, Windows, Linux.

Interactive:
    ./app.py                       # just run it, it asks for URLs

Non-interactive:
    ./app.py "https://www.pornpics.com/galleries/slug-12345/"
    ./app.py "URL1" "URL2" --out "D:/Porn/arietta"
    ./app.py "URL" --max-size 1280 --workers 8 --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error, request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

CDN_IMG_RE = re.compile(r"""https://cdni\.pornpics\.com/\d+/[^"'\s>]+?\.jpe?g""", re.I)
CDN_SIZE_RE = re.compile(r"^(https?://[^/]+/)(\d+)(/.+)$")
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
GALLERY_ID_RE = re.compile(r"-(\d{6,})/?$")

# Largest first: we walk down until the CDN answers with a real file.
SIZE_LADDER = [2560, 2048, 1920, 1600, 1440, 1280, 1024, 800, 640, 460]

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# TLS: find a CA bundle even when the interpreter ships without one
# --------------------------------------------------------------------------
def build_ssl_context(insecure: bool = False) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    candidates = []
    env = os.environ.get("SSL_CERT_FILE")
    if env:
        candidates.append(env)
    try:
        import certifi
        candidates.append(certifi.where())
    except Exception:
        pass
    candidates += [
        "/etc/ssl/cert.pem",                    # macOS (system roots)
        "/etc/ssl/certs/ca-certificates.crt",   # Debian / Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL / Fedora
        "/usr/local/etc/openssl@3/cert.pem",    # Homebrew
    ]
    for c in candidates:
        try:
            if c and os.path.isfile(c):
                return ssl.create_default_context(cafile=c)
        except Exception:
            continue
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


SSL_CTX = build_ssl_context()


# --------------------------------------------------------------------------
# Platform-aware Downloads folder
# --------------------------------------------------------------------------
def _windows_downloads() -> Path | None:
    """Resolve the real Downloads folder on Windows (handles OneDrive redirects)."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        u = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

        ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(ptr)
        )
        if hr == 0 and ptr.value:
            path = Path(ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            if path.is_dir():
                return path
    except Exception:
        return None
    return None


def downloads_dir() -> Path:
    if os.name == "nt":
        p = _windows_downloads()
        if p:
            return p
    p = Path.home() / "Downloads"
    return p if p.is_dir() else Path.home()


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def http_request(url: str, referer: str | None = None, method: str | None = None,
                 timeout: int = 30):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    return request.urlopen(
        request.Request(url, headers=headers, method=method),
        timeout=timeout,
        context=SSL_CTX,
    )


def fetch_html(url: str, retries: int = 4) -> str:
    last = None
    for i in range(retries):
        try:
            with http_request(url, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 + i * 1.5)
    raise SystemExit("Could not fetch {}: {}".format(url, last))


def with_size(url: str, size: int) -> str:
    m = CDN_SIZE_RE.match(url)
    return "{}{}{}".format(m.group(1), size, m.group(3)) if m else url


def probe_best_size(sample_url: str, referer: str, ladder) -> int:
    """Return the largest CDN size that actually returns a file."""
    for size in ladder:
        candidate = with_size(sample_url, size)
        try:
            with http_request(candidate, referer=referer, method="HEAD", timeout=20) as r:
                if r.status == 200:
                    return size
        except error.HTTPError as e:
            if e.code == 405:  # HEAD not allowed -> try a GET
                try:
                    with http_request(candidate, referer=referer, timeout=20):
                        return size
                except Exception:
                    pass
        except Exception:
            continue
    return 1280


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def gallery_id(url: str) -> str | None:
    m = GALLERY_ID_RE.search(url.rstrip("/"))
    return m.group(1) if m else None


def extract_title(html: str) -> str:
    m = TITLE_RE.search(html)
    if not m:
        return "gallery"
    txt = re.sub(r"<[^>]+>", "", m.group(1))
    return re.sub(r"\s+", " ", txt).strip() or "gallery"


def collect_images(html: str, gid: str | None) -> "dict[str, str]":
    """Map filename -> url, optionally restricted to this gallery's ID."""
    out = {}
    for m in CDN_IMG_RE.finditer(html):
        u = m.group(0).rstrip("\"'")
        if gid and "/{}/".format(gid) not in u:
            continue
        name = u.rsplit("/", 1)[-1].split("?")[0]
        out.setdefault(name, u)
    return out


def gather_images(base_url: str, max_pages: int):
    html = fetch_html(base_url)
    title = extract_title(html)
    gid = gallery_id(base_url)

    found = collect_images(html, gid)
    if not found:  # ID filter too strict -> fall back
        found = collect_images(html, None)

    pages_used = 1
    if max_pages > 1:
        for p in range(2, max_pages + 1):
            new = {}
            for cand in ("{}?p={}".format(base_url, p),
                         "{}{}/".format(base_url.rstrip("/") + "/", p)):
                try:
                    page = fetch_html(cand, retries=1)
                except SystemExit:
                    continue
                new = {k: v for k, v in collect_images(page, gid).items() if k not in found}
                if new:
                    break
            if not new:
                break
            found.update(new)
            pages_used = p
    return title, gid, found, pages_used


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------
def download(url: str, dest: Path, referer: str, overwrite: bool, retries: int = 4):
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        return "skip", dest.stat().st_size, None

    tmp = Path(str(dest) + ".part")
    last = None
    for i in range(retries):
        try:
            total = 0
            with http_request(url, referer=referer, timeout=90) as r, open(tmp, "wb") as fh:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            if total == 0:
                raise IOError("zero bytes")
            os.replace(str(tmp), str(dest))
            return "ok", total, None
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 + i)
    try:
        tmp.unlink()
    except OSError:
        pass
    return "fail", 0, last


def safe_name(name: str, limit: int = 90) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return name[:limit] or "gallery"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} TB".format(n)


# --------------------------------------------------------------------------
# Interactive prompt
# --------------------------------------------------------------------------
def clean_url(raw: str) -> str:
    """Tolerate drag-and-drop, quotes, missing scheme, stray whitespace."""
    u = raw.strip().strip('"').strip("'").strip()
    u = u.replace("\\ ", " ").replace("\\", "")
    if u and not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u


def prompt_for_urls() -> list[str]:
    print()
    print("  Paste one or more PornPics gallery URLs.")
    print("  Enter a blank line to start ripping, or 'q' to quit.")
    print()
    urls: list[str] = []
    while True:
        try:
            raw = input("  URL {}: ".format(len(urls) + 1))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw.strip():
            if urls:
                break
            continue
        if raw.strip().lower() in ("q", "quit", "exit"):
            return [] if not urls else urls
        u = clean_url(raw)
        if "pornpics." not in u or "/galleries/" not in u:
            print("      ! doesn't look like a gallery URL, trying anyway")
        urls.append(u)
    return urls


def prompt_out_dir(current: str | None) -> str:
    default = current or str(downloads_dir() / "pornpics")
    try:
        raw = input("  Save to [{}]: ".format(default)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return clean_url(raw) if raw.startswith(("http", "~", "/", ".")) and False else (raw or default)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run_gallery(base_url: str, args) -> None:
    log("\n==> {}".format(base_url))
    title, gid, images, pages_used = gather_images(base_url, args.pages)
    if not images:
        log("    No images found (check the URL).")
        return

    names = sorted(images)
    sample = images[names[0]]

    size = args.max_size or probe_best_size(sample, base_url, SIZE_LADDER)
    urls = [(n, with_size(images[n], size)) for n in names]

    slug = safe_name(base_url.rstrip("/").rsplit("/", 1)[-1])
    folder = Path(args.out).expanduser() / slug if args.out else downloads_dir() / "pornpics" / slug
    folder.mkdir(parents=True, exist_ok=True)

    log("    Title    : {}".format(title[:90]))
    log("    Gallery  : {} | {} images | {} page(s)".format(gid or "?", len(urls), pages_used))
    log("    Max size : {} px".format(size))
    log("    Saving to: {}".format(folder))

    if args.dry_run:
        for n, u in urls:
            print("      {}  {}".format(n, u))
        return

    total_bytes = 0
    ok = skipped = failed = 0
    done = 0
    started = time.time()

    def task(item):
        nonlocal total_bytes, ok, skipped, failed, done
        name, url = item
        status, nbytes, err = download(url, folder / name, base_url, args.overwrite)
        with _print_lock:
            done += 1
            if status == "ok":
                ok += 1
                total_bytes += nbytes
                log("    [{:>3}/{}] OK   {:<40} {:>9}".format(done, len(urls), name[:40], human(nbytes)))
            elif status == "skip":
                skipped += 1
                log("    [{:>3}/{}] SKIP {:<40} (already have)".format(done, len(urls), name[:40]))
            else:
                failed += 1
                log("    [{:>3}/{}] FAIL {:<40} {}".format(done, len(urls), name[:40], err))

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(task, urls))

    # A manifest so you can re-run / archive later
    try:
        (folder / "_gallery.txt").write_text(
            "title: {}\nurl: {}\ngallery_id: {}\nimages: {}\nsize: {}px\n\n{}".format(
                title, base_url, gid or "", len(urls), size,
                "\n".join(u for _, u in urls)),
            encoding="utf-8")
    except OSError:
        pass

    log("\n    Done: {} downloaded, {} skipped, {} failed | {} in {:.1f}s".format(
        ok, skipped, failed, human(total_bytes), time.time() - started))
    if failed:
        log("    Re-run the same command to retry the failed ones.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Download all hi-res images from PornPics galleries. "
                    "Run with no arguments for interactive mode.")
    ap.add_argument("urls", nargs="*", help="one or more gallery URLs (omit to be prompted)")
    ap.add_argument("--out", help="output root folder (default: <Downloads>/pornpics)")
    ap.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    ap.add_argument("--max-size", type=int, default=0,
                    help="force a CDN width, e.g. 1280 (default: auto-detect largest)")
    ap.add_argument("--pages", type=int, default=6,
                    help="max gallery sub-pages to probe, 1 disables (default 6)")
    ap.add_argument("--overwrite", action="store_true", help="re-download existing files")
    ap.add_argument("--dry-run", action="store_true", help="list URLs without downloading")
    ap.add_argument("--once", action="store_true",
                    help="interactive mode: exit after one batch instead of asking again")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (fixes local CA bundle issues)")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    if args.insecure:
        global SSL_CTX
        SSL_CTX = build_ssl_context(insecure=True)

    interactive = not args.urls
    if interactive:
        print()
        print("  PornPics Gallery Ripper")
        print("  -----------------------")

    first_round = True
    while True:
        urls = args.urls if first_round and args.urls else prompt_for_urls()
        first_round = False

        if not urls:
            if interactive:
                print("  Nothing to do. Bye.")
            return 0

        if interactive and not args.out and not args.dry_run:
            args.out = prompt_out_dir(args.out)

        for u in urls:
            try:
                run_gallery(u, args)
            except KeyboardInterrupt:
                log("\n    Interrupted.")
                return 130
            except SystemExit as e:
                log("    {}".format(e))

        if not interactive or args.once:
            return 0

        try:
            again = input("\n  Rip another? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if again.startswith("n"):
            print("  Bye.")
            return 0


if __name__ == "__main__":
    sys.exit(main())

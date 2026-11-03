import hashlib
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse
import trafilatura
from . import register_tool

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

KNOWN_VIDEO_DOMAINS = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "x.com",
    "twitter.com", "reddit.com", "v.redd.it", "vimeo.com", "twitch.tv",
    "dailymotion.com", "facebook.com", "fb.watch", "soundcloud.com",
    "streamable.com", "bilibili.com", "rumble.com"
)

AD_BLACKLIST = (
    "doubleclick", "googlesyndication", "exoclick", "juicyads",
    "trafficjunky", "popads", "adsco", "tsyndicate", "previews",
    "thumb", "banner", "analytics", "pixel", "favicon", "google-analytics"
)

DIRECT_MEDIA_EXTS = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".mp3", ".m4a", ".wav", ".m3u8", ".mpd")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _probe_url_head(url: str) -> tuple[bool, str]:
    lower_path = urlparse(url).path.lower()
    if lower_path.endswith(DIRECT_MEDIA_EXTS):
        return True, "direct_extension"

    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": DEFAULT_UA},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if any(m in ctype for m in ("video/", "audio/", "mpegurl", "dash+xml", "octet-stream")):
                return True, ctype
            return False, ctype
    except Exception:
        return False, "unknown"


def _extract_media_from_html(page_url: str, html: str) -> tuple[list[str], str]:
    """
    Fast static regex scanner that extracts video streams and embed iframes
    directly from page HTML without needing Playwright.
    """
    if not html:
        return [], ""

    # 1. Page Title
    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.DOTALL)
    raw_title = title_match.group(1).strip() if title_match else ""
    if not raw_title:
        og_t = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html, re.I)
        raw_title = og_t.group(1).strip() if og_t else ""

    clean_title = re.sub(r'[\\/*?:"<>|]', "_", raw_title).strip()[:65] if raw_title else ""

    candidates = []

    # 2. Direct media files (.mp4, .m3u8, .webm, etc.) in text or scripts
    raw_streams = re.findall(
        r'https?://[^\s"\'<>]+\.(?:mp4|m4v|webm|m3u8)(?:\?[^\s"\'<>]*)?', html, re.I
    )
    for s in raw_streams:
        cleaned = s.replace("\\/", "/").rstrip("\\/'\"")
        if not any(bad in cleaned.lower() for bad in AD_BLACKLIST) and cleaned not in candidates:
            candidates.append(cleaned)

    # 3. OpenGraph & Twitter video meta tags
    og_vids = re.findall(
        r'<meta\s+(?:property|name)=["\'](?:og:video|og:video:url|og:video:secure_url|twitter:player:stream)["\']\s+content=["\'](.*?)["\']',
        html,
        re.I,
    )
    for v in og_vids:
        cleaned = v.replace("\\/", "/").rstrip("\\/'\"")
        if cleaned.startswith("http") and cleaned not in candidates:
            candidates.append(cleaned)

    # 4. Embedded player iframes (e.g. downloaddirect, streamtape, dood, etc.)
    iframe_srcs = re.findall(r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']', html, re.I)
    for ifr in iframe_srcs:
        cleaned = ifr.replace("\\/", "/").rstrip("\\/'\"")
        if not any(bad in cleaned.lower() for bad in AD_BLACKLIST):
            if any(p in cleaned.lower() for p in ("embed", "player", "video", "downloaddirect", "stream", "media")):
                if cleaned not in candidates:
                    candidates.append(cleaned)

    # 5. <video ... src="..."> and <source ... src="...">
    dom_srcs = re.findall(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', html, re.I)
    for ds in dom_srcs:
        full_url = urljoin(page_url, ds.replace("\\/", "/").strip())
        if full_url.startswith("http") and full_url not in candidates:
            if not any(bad in full_url.lower() for bad in AD_BLACKLIST):
                candidates.append(full_url)

    return candidates, clean_title


def _download_direct_http(stream_url: str, out_path: Path, referer: str, user_agent: str = DEFAULT_UA) -> bool:
    """
    Reliable chunked HTTP downloader for direct media files when yt-dlp encounters issues.
    """
    try:
        req = urllib.request.Request(
            stream_url,
            headers={
                "User-Agent": user_agent or DEFAULT_UA,
                "Referer": referer,
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_read = 0
            with open(out_path, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_read += len(chunk)
            if total_read > 250_000:
                return True
            if out_path.exists():
                out_path.unlink()
            return False
    except Exception:
        if out_path.exists():
            out_path.unlink()
        return False


def sniff_media_streams_with_browser(page_url: str, timeout_ms: int = 25000) -> tuple[list[dict], str, str]:
    captured = []
    seen_urls = set()
    url_hash = hashlib.md5(page_url.encode()).hexdigest()[:6]
    page_title = f"Media_{url_hash}"
    rendered_html = ""

    def inspect_response(res):
        try:
            url = res.url
            lower = url.lower()
            if any(bad in lower for bad in AD_BLACKLIST):
                return
            if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".js", ".css", ".woff", ".woff2", ".ico")):
                return

            headers = res.headers
            ctype = headers.get("content-type", "").lower()
            clen = int(headers.get("content-length", "0") or 0)

            is_hls = ".m3u8" in lower or "mpegurl" in ctype
            is_dash = ".mpd" in lower or "dash+xml" in ctype
            is_video_file = bool(re.search(r"\.(mp4|m4v|webm|mov)(\?|$)", lower)) or "video/" in ctype

            if is_video_file and 0 < clen < 250_000:
                return

            if (is_hls or is_dash or is_video_file) and url not in seen_urls:
                seen_urls.add(url)
                priority = 1 if is_hls else (2 if is_video_file else 3)
                captured.append({
                    "url": url,
                    "priority": priority,
                    "headers": res.request.headers,
                })
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()
        page.on("response", inspect_response)

        try:
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass  # Do not abort on navigation timeout; partial page may still contain the player!

            page.wait_for_timeout(2000)

            # Scroll down to trigger loading="lazy" iframes and video elements
            try:
                page.evaluate("() => { window.scrollBy(0, 800); }")
                page.wait_for_timeout(1000)
                page.evaluate("() => { window.scrollBy(0, 1600); }")
                page.wait_for_timeout(1000)
            except Exception:
                pass

            raw_title = page.title()
            if raw_title:
                clean_t = re.sub(r'[\\/*?:"<>|]', "_", raw_title).strip()[:65]
                page_title = f"{clean_t} [{url_hash}]"

            # Check for play buttons or players and click if needed
            for sel in [".vjs-big-play-button", ".plyr__control--overlaid", "[aria-label*='Play']", ".play-btn", "video"]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=800, force=True)
                        page.wait_for_timeout(1500)
                        if captured:
                            break
                except Exception:
                    continue

            # Inspect all frames (including child iframes!) for video/source elements
            for frame in page.frames:
                try:
                    f_srcs = frame.evaluate("""() => {
                        const u = [];
                        document.querySelectorAll('video, source, a[href*=".mp4"]').forEach(el => {
                            if (el.src && el.src.startsWith('http')) u.push(el.src);
                            if (el.currentSrc && el.currentSrc.startsWith('http')) u.push(el.currentSrc);
                            if (el.href && el.href.startsWith('http')) u.push(el.href);
                        });
                        return u;
                    }""")
                    for s_url in f_srcs:
                        if s_url not in seen_urls and not any(b in s_url.lower() for b in AD_BLACKLIST):
                            seen_urls.add(s_url)
                            captured.append({"url": s_url, "priority": 2, "headers": {}})
                except Exception:
                    pass

            rendered_html = page.content()
        except Exception:
            pass
        finally:
            browser.close()

    captured.sort(key=lambda x: x["priority"])
    return captured, page_title, rendered_html


def _process_single_url(url: str, mode: str, out_dir: Path, obs_limit: int, is_recursive: bool = False) -> str:
    parsed_uri = urlparse(url)
    domain = parsed_uri.netloc.lower()
    origin = f"{parsed_uri.scheme}://{parsed_uri.netloc}"

    def build_opts(custom_title: str = None, extra_headers: dict = None) -> dict:
        tmpl = f"{custom_title}.%(ext)s" if custom_title else "%(title).75B [%(id)s].%(ext)s"
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": url,
            "Origin": origin,
        }
        if extra_headers:
            for k, v in extra_headers.items():
                if k.lower() in ("referer", "user-agent", "cookie", "origin"):
                    headers[k] = v

        opts = {
            "outtmpl": str(out_dir / tmpl),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 6,
            "http_headers": headers,
        }
        if mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            opts["merge_output_format"] = "mp4"
        return opts

    is_known_platform = any(kd in domain for kd in KNOWN_VIDEO_DOMAINS)
    is_direct_media, detected_ctype = _probe_url_head(url)

    # 1. Direct platform / media file download via yt-dlp
    if is_known_platform or is_direct_media:
        try:
            with yt_dlp.YoutubeDL(build_opts()) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = ydl.prepare_filename(info)
                if not Path(fp).exists() and Path(fp).with_suffix(".mp4").exists():
                    fp = str(Path(fp).with_suffix(".mp4"))
                return f"[DOWNLOAD_SUCCESS] {url} -> Saved to {fp}"
        except Exception:
            if is_known_platform:
                for br in ("chrome", "safari", "brave"):
                    try:
                        c_opts = build_opts()
                        c_opts["cookiesfrombrowser"] = (br,)
                        with yt_dlp.YoutubeDL(c_opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                            fp = ydl.prepare_filename(info)
                            return f"[DOWNLOAD_SUCCESS via {br} cookies] {url} -> Saved to {fp}"
                    except Exception:
                        continue

    # 2. Fast Static HTML Analysis (Extract streams without heavy browser overhead)
    static_html = ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Referer": origin,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            static_html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        pass

    if static_html:
        static_candidates, static_title = _extract_media_from_html(url, static_html)
        title_for_file = static_title or f"Media_{hashlib.md5(url.encode()).hexdigest()[:6]}"
        for cand in static_candidates[:4]:
            try:
                s_opts = build_opts(custom_title=title_for_file)
                with yt_dlp.YoutubeDL(s_opts) as ydl:
                    info = ydl.extract_info(cand, download=True)
                    fp = ydl.prepare_filename(info)
                    if not Path(fp).exists() and Path(fp).with_suffix(".mp4").exists():
                        fp = str(Path(fp).with_suffix(".mp4"))
                    return f"[DOWNLOAD_SUCCESS via Page Stream] {url}\n- Source: {cand}\n- Saved to: {fp}"
            except Exception:
                if cand.lower().endswith((".mp4", ".m4v", ".webm")):
                    target_fp = out_dir / f"{title_for_file}.mp4"
                    if _download_direct_http(cand, target_fp, referer=url):
                        return f"[DOWNLOAD_SUCCESS via Direct Download] {url}\n- Source: {cand}\n- Saved to: {target_fp}"

        # If no direct video streams found, check if this is a collection, feed, or category page with video post links
        if not is_recursive and not static_candidates:
            post_patterns = [
                r'href=[\"\'](/post/[^\"\'#?]+|https?://[^\s\"\'<>]*/post/[^\"\'#?]+)[\"\']',
                r'href=[\"\'](/video/[^\"\'#?]+|https?://[^\s\"\'<>]*/video/[^\"\'#?]+)[\"\']',
                r'href=[\"\'](/watch\?[^\"\'#?]+|https?://[^\s\"\'<>]*/watch\?[^\"\'#?]+)[\"\']',
                r'href=[\"\'](/v/[^\"\'#?]+|https?://[^\s\"\'<>]*/v/[^\"\'#?]+)[\"\']',
            ]
            discovered_posts = []
            for pat in post_patterns:
                for m in re.findall(pat, static_html, re.I):
                    full_u = urljoin(url, m.strip())
                    if full_u != url and full_u not in discovered_posts and not any(bad in full_u.lower() for bad in AD_BLACKLIST):
                        discovered_posts.append(full_u)

            if discovered_posts:
                crawled_results = []
                for post_u in discovered_posts[:2]:
                    sub_res = _process_single_url(post_u, mode, out_dir, obs_limit, is_recursive=True)
                    crawled_results.append(f"- {sub_res}")
                return f"[DOWNLOAD_SUCCESS via Site Crawl] Found video posts on {url}:\n" + "\n".join(crawled_results)

    # 3. Dynamic Playwright Sniffer (For JS-obfuscated or dynamically rendered players)
    if HAS_PLAYWRIGHT:
        streams, sniffed_title, rendered_html = sniff_media_streams_with_browser(url)
        active_html = rendered_html or static_html

        if streams:
            for st in streams[:4]:
                stream_u = st["url"]
                try:
                    s_opts = build_opts(custom_title=sniffed_title, extra_headers=st.get("headers"))
                    with yt_dlp.YoutubeDL(s_opts) as ydl:
                        info = ydl.extract_info(stream_u, download=True)
                        fp = ydl.prepare_filename(info)
                        if not Path(fp).exists() and Path(fp).with_suffix(".mp4").exists():
                            fp = str(Path(fp).with_suffix(".mp4"))
                        return (
                            f"[DOWNLOAD_SUCCESS via Stream Sniffer] {url}\n"
                            f"- Saved to: {fp}"
                        )
                except Exception:
                    if stream_u.lower().endswith((".mp4", ".m4v", ".webm")):
                        target_fp = out_dir / f"{sniffed_title}.mp4"
                        if _download_direct_http(stream_u, target_fp, referer=url):
                            return f"[DOWNLOAD_SUCCESS via Direct Sniffer Download] {url}\n- Saved to: {target_fp}"

        # If still no direct video stream, check rendered HTML for video post links
        if not is_recursive and active_html:
            post_patterns = [
                r'href=[\"\'](/post/[^\"\'#?]+|https?://[^\s\"\'<>]*/post/[^\"\'#?]+)[\"\']',
                r'href=[\"\'](/video/[^\"\'#?]+|https?://[^\s\"\'<>]*/video/[^\"\'#?]+)[\"\']',
            ]
            discovered_posts = []
            for pat in post_patterns:
                for m in re.findall(pat, active_html, re.I):
                    full_u = urljoin(url, m.strip())
                    if full_u != url and full_u not in discovered_posts and not any(bad in full_u.lower() for bad in AD_BLACKLIST):
                        discovered_posts.append(full_u)

            if discovered_posts:
                crawled_results = []
                for post_u in discovered_posts[:2]:
                    sub_res = _process_single_url(post_u, mode, out_dir, obs_limit, is_recursive=True)
                    crawled_results.append(f"- {sub_res}")
                return f"[DOWNLOAD_SUCCESS via Site Crawl] Found video posts on {url}:\n" + "\n".join(crawled_results)

        extracted_text = ""
        if active_html:
            extracted_text = trafilatura.extract(active_html, include_formatting=True, include_links=True) or ""
        if not extracted_text:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                extracted_text = trafilatura.extract(downloaded, include_formatting=True, include_links=True) or ""

        snippet = extracted_text[:obs_limit] if extracted_text else "No readable article text found on page."
        return (
            f"[TERMINAL_NO_MEDIA_FOUND] Inspected {url} (Content-Type: {detected_ctype}). "
            f"There are NO downloadable video or audio streams on this webpage.\n\n"
            f"Webpage text content:\n{snippet}"
        )

    return f"[TERMINAL_ERROR] No video stream found at {url}."


@register_tool(
    name="download_media",
    description="Download one or multiple video/audio URLs (.mp4/.m3u8/Reels/YouTube) into Workspace",
    schema_example='{"tool": "download_media", "urls": ["https://..."], "mode": "video"}',
)
def tool_download_media(payload: dict, context: dict) -> str:
    if not HAS_YTDLP:
        return "Error: yt-dlp is not installed."

    mode = payload.get("mode", "video").strip().lower()
    obs_limit = int(context.get("obs_limit", 4000))
    raw_urls = []

    if isinstance(payload.get("urls"), list):
        raw_urls.extend(str(u).strip() for u in payload["urls"])
    elif isinstance(payload.get("urls"), str):
        raw_urls.extend(re.findall(r"https?://[^\s,;\"]+", payload["urls"]))

    if payload.get("url"):
        raw_urls.extend(re.findall(r"https?://[^\s,;\"]+", str(payload["url"])))

    unique_urls = list(dict.fromkeys(u for u in raw_urls if u.startswith("http")))
    if not unique_urls:
        return "[TERMINAL_ERROR] No valid http/https URLs provided."

    out_dir = Path(context.get("cwd", "./Workspace")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for idx, u in enumerate(unique_urls, 1):
        res_str = _process_single_url(u, mode, out_dir, obs_limit)
        results.append(f"({idx}/{len(unique_urls)}) {res_str}")

    return "\n\n".join(results)

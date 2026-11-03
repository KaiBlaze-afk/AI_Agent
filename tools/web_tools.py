import json
import trafilatura
from ddgs import DDGS
from . import register_tool
from .media_tools import _probe_url_head, tool_download_media


@register_tool(
    name="web_search",
    description="Search live internet for facts, songs, lyrics, URLs, or general information",
    schema_example='{"tool": "web_search", "query": "precise search keywords"}',
)
def tool_web_search(payload: dict, context: dict) -> str:
    query = payload.get("query", "").strip()
    max_results = int(payload.get("max_results", 5))
    if not query:
        return "[TERMINAL_ERROR] Missing 'query' field."

    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "[TERMINAL_NO_RESULTS] No search results found for that query."
        blocks = []
        for idx, r in enumerate(results, 1):
            blocks.append(
                f"[{idx}] {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}"
            )
        return "\n\n".join(blocks)


@register_tool(
    name="image_search",
    description="Fetch and display visual images/photos/art in the chat UI when the user asks to see an image or asks what a visual entity looks like",
    schema_example='{"tool": "image_search", "query": "Raiden Shogun Genshin Impact official art", "count": 3}',
)
def tool_image_search(payload: dict, context: dict) -> str:
    query = payload.get("query", "").strip()
    count = min(int(payload.get("count", 3)), 6)
    if not query:
        return "[TERMINAL_ERROR] Missing 'query' field for image_search."

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.images(query, max_results=count + 4))
            if not raw_results:
                return f"[TERMINAL_NO_RESULTS] No images found for '{query}'."

            gallery = []
            for r in raw_results:
                img_url = r.get("image")
                thumb_url = r.get("thumbnail")
                title = r.get("title", query)
                source = r.get("url", "")
                if img_url and img_url.startswith("http"):
                    gallery.append({
                        "title": title,
                        "image": img_url,
                        "thumbnail": thumb_url or img_url,
                        "source": source,
                    })
                if len(gallery) >= count:
                    break

            if not gallery:
                return f"[TERMINAL_NO_RESULTS] Could not extract valid image URLs for '{query}'."

            gallery_json = json.dumps(gallery)
            titles_summary = ", ".join(g["title"][:45] for g in gallery)
            return (
                f"[IMAGE_GALLERY_JSON]{gallery_json}[/IMAGE_GALLERY_JSON]\n"
                f"[IMAGE_SEARCH_SUCCESS] Displayed {len(gallery)} image(s) of '{query}' ({titles_summary}) directly on the user's screen."
            )
    except Exception as e:
        return f"[TERMINAL_ERROR] Image search error: {str(e)}"


@register_tool(
    name="fetch_webpage",
    description="Read full text/articles/lyrics from a webpage URL",
    schema_example='{"tool": "fetch_webpage", "url": "https://example.com/article"}',
)
def tool_fetch_webpage(payload: dict, context: dict) -> str:
    url = payload.get("url", "").strip()
    max_chars = int(context.get("obs_limit", 4000))
    if not url:
        return "[TERMINAL_ERROR] Missing 'url' field."

    is_media, ctype = _probe_url_head(url)
    if is_media:
        return tool_download_media({"urls": [url], "mode": "video"}, context)

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return f"[TERMINAL_ERROR] Could not download webpage at {url}."
    text = trafilatura.extract(
        downloaded, include_formatting=True, include_links=True, include_tables=True
    )
    if not text:
        return f"[TERMINAL_NO_TEXT] Webpage {url} loaded (Content-Type: {ctype}), but contains no readable article text."
    return f"[WEBPAGE_CONTENT from {url}]:\n{text[:max_chars]}"
---
name: image-search-usage
description: Search the web for images with filters and download them. Use when you need stock photos, reference images, or product shots from the web.
---

## Image Search Tools

You have three image-related tools alongside the `display_images` tool from display-mcp:

### `find_images(query, count=3, prefer_provider="all", orientation="")`

Search the web for images matching `query`. Returns up to `count` candidates.

- `prefer_provider`:
  - `"stock"` — free Unsplash + Pexels only. Best for scenic / object / lifestyle / abstract queries. **Costs nothing.**
  - `"google"` — real Google Image Search results (across the entire web) via SerpAPI's `google_images` engine. Best for specific real-world things (people, news, products, brands). **Paid (~$0.01/call).**
  - `"all"` (default) — every configured provider in parallel. Best coverage. **Paid (Google contributes via SerpAPI).**
- `orientation`: `""` (any), `"landscape"`, `"portrait"`, `"square"`.

Returns:
```json
{
  "images": [
    {"url": "https://...", "source": "unsplash",
     "attribution": "Photo by Jane Doe",
     "source_page": "https://unsplash.com/@jane",
     "width": 1080, "height": 720,
     "thumbnail_url": "https://..."}
  ]
}
```

If no provider keys are configured: returns an `{"error": "..."}` you should surface to the user verbatim — they need an admin to add API keys.

**Pick a few good results, don't dump all of them.** Then call `display_images` from display-mcp:
```
display_images(images=[
  {"source": hit["url"],
   "caption": hit["attribution"],
   "attribution": hit["source"],
   "link_url": hit["source_page"]}
  for hit in result["images"][:3]
])
```

### `search_by_image(image_path, mode="all", count=10)`

Reverse image search via Google Lens (powered by SerpAPI). Use this when the user uploads a photo and asks what it is, where to buy it, where it appears online, etc.

- `image_path`: relative-to-workspace (e.g. `"uploads/photos/sweater.jpg"`) OR sandbox-absolute path (e.g. `"/users/{u}/workspace/uploads/photos/sweater.jpg"`).
- `mode`:
  - `"products"` — best for "where can I buy this?" — surfaces shopping results with prices and retailer links.
  - `"visual_matches"` — similar-looking images on the web.
  - `"exact_matches"` — finds copies of the exact same image (good for provenance / source-tracking).
  - `"all"` (default) — mix of the above.
- `count`: max results.

Returns:
```json
{
  "matches": [
    {"title": "Cozy wool sweater",
     "link": "https://www.hm.com/...",
     "source_url": "hm.com",
     "thumbnail_url": "https://...",
     "price": "45.00", "currency": "USD",
     "source_name": "H&M"}
  ]
}
```

**For shopping queries**, render the matches as a clickable gallery so the user can tap straight through to retailer pages:
```
display_images(images=[
  {"source": match["thumbnail_url"],
   "caption": match["title"],
   "attribution": (f"{match['price']} {match['currency']} — {match['source_name']}"
                   if match.get("price") else match.get("source_name", "")),
   "link_url": match["link"]}     # ← clickable card opens the retailer page
  for match in result["matches"][:6]
])
```

If SerpAPI isn't configured, returns an `{"error": "..."}` — surface it to the user.

### `save_image(url, dest_path="", alt_text="")`

Download an image URL and save it to the agent's workspace. Use only when the user explicitly asks to save an image (e.g. "save the second one to my photos folder").

- `url`: HTTPS URL to download.
- `dest_path`:
  - Empty (default) → auto-generates `images/saved_<uuid>.<ext>`.
  - Relative (e.g. `"iceland/landscape1.jpg"`) → joined under your workspace root.
  - Sandbox-absolute (e.g. `"/users/{u}/workspace/photos/x.jpg"`) → validated to stay inside your writable scope.
- `alt_text`: optional alt text (reserved for future sidecar `.alt` files).

Returns:
```json
{"saved_path": "/users/.../iceland.jpg",
 "size_bytes": 524288,
 "mime_type": "image/jpeg",
 "width": 1920, "height": 1080}
```

Errors clearly if the URL isn't an image, the file is > 15MB, or the destination is outside your writable scope.

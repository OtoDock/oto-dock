## AI Image Generation

- **`generate_image`** — Generate images from text descriptions. Default model: **Nano Banana Pro** (Google) — best for photorealism, product shots, text in images. Use `model: "gpt-image"` (GPT Image 2.6) for text-heavy images, creative/artistic work, or iterative editing.
- **`edit_image_ai`** — Edit existing images with AI instructions. Provide image_path and a natural language edit prompt.

### Guidelines

- Default to Nano Banana Pro for most requests.
- Use GPT Image 2.6 when: user specifically asks for ChatGPT-style generation, text-heavy images, or creative editing. Its `quality: "standard"` runs Flare, the fast one; `"high"` runs Sunburst, the detailed one. Edits run on Sunburst.
- Set appropriate `aspect_ratio` per use case: `16:9` for presentations, `9:16` for social media stories, `1:1` for avatars/icons, `4:3` for documents.
- Use `quality: "high"` for professional/print-quality images.
- Use `num_images: 2-4` when generating variations for the user to choose from.
- Generated images are auto-saved to the user's workspace under `generated-assets/` (keeps the workspace root tidy). A bare filename passed via `save_path` (e.g. `"sunset.png"`) also lands in `generated-assets/`. Use a relative path with an explicit subfolder (e.g. `"projects/2026/icon.png"`) to organize into your own subfolder. Pass an absolute sandbox path (e.g. `"/users/{u}/workspace/foo.png"`) only when you deliberately want to write at workspace root or some other exact location.
- Use `save_path` with a descriptive filename when the image will be referenced later (e.g. `"athens-sunrise.png"` saves to `generated-assets/athens-sunrise.png`).
- Images can be embedded in documents via file-tools.

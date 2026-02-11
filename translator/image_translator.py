"""Image translation module — OCR Japanese text from game images, translate, render English.

Pipeline: Qwen3-VL (OCR + bounding boxes) → Sugoi/Qwen3 (translation) → Pillow (render).
Ollama auto-swaps models between the vision and text phases.
"""

import base64
import json
import os
import re
from dataclasses import dataclass, field
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont


# ── Image file extensions ────────────────────────────────────────

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
ENCRYPTED_EXTS = (".rpgmvp", ".png_")  # MV/MZ encrypted image formats
ALL_IMAGE_EXTS = IMAGE_EXTS + ENCRYPTED_EXTS

# RPG Maker MV/MZ encrypted file header length
_RPGMV_HEADER_LEN = 16


# ── RPG Maker MV/MZ encryption support ──────────────────────────

def read_encryption_key(project_dir: str) -> str:
    """Read encryptionKey from System.json (MV/MZ encrypted deployments)."""
    for candidate in (
        os.path.join(project_dir, "data", "System.json"),
        os.path.join(project_dir, "Data", "System.json"),
        os.path.join(project_dir, "www", "data", "System.json"),
        os.path.join(project_dir, "www", "Data", "System.json"),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    system = json.load(f)
                key = system.get("encryptionKey", "")
                if key:
                    return key
            except (json.JSONDecodeError, OSError):
                continue
    return ""


def decrypt_rpgmvp(file_path: str, encryption_key: str) -> bytes:
    """Decrypt an .rpgmvp / .png_ file to raw PNG bytes.

    RPG Maker MV/MZ encryption format:
    - First 16 bytes: RPG Maker header (signature + version)
    - Remaining bytes: original PNG with first 16 bytes XOR'd with key
    """
    key_bytes = bytes.fromhex(encryption_key)

    with open(file_path, "rb") as f:
        data = f.read()

    # Skip the 16-byte RPG Maker header
    encrypted = data[_RPGMV_HEADER_LEN:]

    # XOR the first 16 bytes with the key to restore PNG header
    decrypted_head = bytes(b ^ k for b, k in zip(encrypted[:16], key_bytes))

    # Rest of the file is unencrypted
    return decrypted_head + encrypted[16:]


# ── Data classes ──────────────────────────────────────────────────

@dataclass
class TextRegion:
    """A detected Japanese text region in an image."""
    text: str                          # Original Japanese text
    bbox: tuple[int, int, int, int]    # (x1, y1, x2, y2) pixels from top-left
    translation: str = ""


@dataclass
class ImageResult:
    """Result of processing a single image."""
    source_path: str
    output_path: str = ""
    regions: list[TextRegion] = field(default_factory=list)
    skipped: bool = False              # True if no JP text found
    error: str = ""


# ── OCR prompt ────────────────────────────────────────────────────

_OCR_SYSTEM = "You are an OCR system for game screenshots. Extract all Japanese text visible in the image."

_OCR_USER = """\
Find ALL Japanese text in this game image.
For each text region, return a JSON array:
[{"text": "Japanese text here", "bbox": [x1, y1, x2, y2]}, ...]
Coordinates are pixel positions from the top-left corner of the image.
Only include regions containing Japanese characters (hiragana, katakana, or kanji).
Ignore English text, numbers only, and decorative/ornamental elements.
If no Japanese text is found, return: []
Return ONLY the JSON array, no other text."""

# Regex to extract a JSON array from LLM output (may have markdown fences)
_JSON_RE = re.compile(r'\[.*\]', re.DOTALL)


# ── Font discovery ────────────────────────────────────────────────

def _find_font() -> str | None:
    """Find a usable TrueType font on the system."""
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        # Linux / macOS fallbacks
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_SYSTEM_FONT = _find_font()


# ── ImageTranslator ──────────────────────────────────────────────

class ImageTranslator:
    """Scans game images, OCRs Japanese text, translates, and renders English."""

    # Subdirectories most likely to contain translatable text
    PRIORITY_DIRS = {"pictures", "titles1", "titles2", "system"}
    # Subdirectories that rarely contain text (skip by default)
    SKIP_DIRS = {
        "faces", "characters", "sv_actors", "sv_enemies",
        "enemies", "battlebacks1", "battlebacks2",
        "parallaxes", "tilesets", "animations",
    }

    def __init__(self, ollama_url: str, vision_model: str, text_client,
                 encryption_key: str = ""):
        """
        Args:
            ollama_url: Ollama base URL (e.g. http://localhost:11434).
            vision_model: Name of the vision model (e.g. qwen3-vl:8b).
            text_client: An OllamaClient instance for text translation (Sugoi/Qwen3).
            encryption_key: RPG Maker MV/MZ encryption key (hex string from System.json).
        """
        self.ollama_url = ollama_url.rstrip("/")
        self.vision_model = vision_model
        self.text_client = text_client
        self.encryption_key = encryption_key

    # ── Image loading (handles encrypted files) ─────────────────

    def open_image(self, image_path: str) -> Image.Image:
        """Open an image file, decrypting .rpgmvp/.png_ if needed."""
        if (image_path.lower().endswith(ENCRYPTED_EXTS)
                and self.encryption_key):
            raw = decrypt_rpgmvp(image_path, self.encryption_key)
            return Image.open(BytesIO(raw))
        return Image.open(image_path)

    # ── Scanning ──────────────────────────────────────────────────

    @staticmethod
    def find_img_dir(project_dir: str) -> str | None:
        """Locate the img/ directory — handles both root and www/ layouts."""
        for candidate in (
            os.path.join(project_dir, "img"),
            os.path.join(project_dir, "www", "img"),
        ):
            if os.path.isdir(candidate):
                return candidate
        return None

    def scan_images(self, project_dir: str, subdirs: list[str]) -> list[str]:
        """Find all images (including encrypted .rpgmvp) in selected img/ subdirectories."""
        img_dir = self.find_img_dir(project_dir)
        if not img_dir:
            return []
        results = []
        for subdir in subdirs:
            folder = os.path.join(img_dir, subdir)
            if not os.path.isdir(folder):
                continue
            for fname in sorted(os.listdir(folder)):
                if fname.lower().endswith(ALL_IMAGE_EXTS):
                    results.append(os.path.join(folder, fname))
        return results

    @staticmethod
    def list_subdirs(project_dir: str) -> list[tuple[str, int]]:
        """List img/ subdirectories with image counts.

        Returns list of (subdir_name, image_count) tuples.
        """
        img_dir = ImageTranslator.find_img_dir(project_dir)
        if not img_dir:
            return []
        results = []
        for name in sorted(os.listdir(img_dir)):
            path = os.path.join(img_dir, name)
            if not os.path.isdir(path):
                continue
            count = sum(
                1 for f in os.listdir(path)
                if f.lower().endswith(ALL_IMAGE_EXTS)
            )
            if count > 0:
                results.append((name, count))
        return results

    # ── OCR via vision model ──────────────────────────────────────

    def ocr_image(self, image_path: str, max_dim: int = 1024) -> list[TextRegion]:
        """Send image to vision model, extract Japanese text + bounding boxes."""
        img = self.open_image(image_path)
        orig_w, orig_h = img.size

        # Resize for VRAM efficiency (OCR doesn't need full 4K)
        scale = 1.0
        longest = max(orig_w, orig_h)
        if longest > max_dim:
            scale = max_dim / longest
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Convert to base64
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # Call vision model
        raw = self._vision_chat(b64, _OCR_USER)

        # Parse response
        regions = self._parse_ocr_response(raw)

        # Scale bounding boxes back to original image dimensions
        if scale != 1.0 and regions:
            inv = 1.0 / scale
            for r in regions:
                x1, y1, x2, y2 = r.bbox
                r.bbox = (
                    int(x1 * inv), int(y1 * inv),
                    int(x2 * inv), int(y2 * inv),
                )

        # Clamp bboxes to image bounds
        for r in regions:
            x1, y1, x2, y2 = r.bbox
            r.bbox = (
                max(0, min(x1, orig_w)),
                max(0, min(y1, orig_h)),
                max(0, min(x2, orig_w)),
                max(0, min(y2, orig_h)),
            )

        # Filter out zero-area or tiny regions
        regions = [r for r in regions if
                   (r.bbox[2] - r.bbox[0]) > 5 and (r.bbox[3] - r.bbox[1]) > 5]

        return regions

    def _vision_chat(self, image_base64: str, prompt: str) -> str:
        """Send an image to the vision model via Ollama /api/chat."""
        payload = {
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": _OCR_SYSTEM},
                {"role": "user", "content": prompt, "images": [image_base64]},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 4096},
        }
        resp = requests.post(
            f"{self.ollama_url}/api/chat",
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    @staticmethod
    def _parse_ocr_response(raw: str) -> list[TextRegion]:
        """Parse vision model JSON output into TextRegion list."""
        # Try direct parse first
        text = raw.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        # Extract JSON array
        m = _JSON_RE.search(text)
        if not m:
            return []

        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []

        regions = []
        for item in data:
            if not isinstance(item, dict):
                continue
            t = item.get("text", "").strip()
            bbox = item.get("bbox", [])
            if not t or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            try:
                coords = tuple(int(v) for v in bbox)
            except (ValueError, TypeError):
                continue
            regions.append(TextRegion(text=t, bbox=coords))

        return regions

    # ── Translation ───────────────────────────────────────────────

    def translate_regions(self, regions: list[TextRegion]) -> list[TextRegion]:
        """Translate each region's Japanese text using the text translation model."""
        for region in regions:
            if not region.text:
                continue
            try:
                result = self.text_client.translate_name(
                    region.text, hint="text in game image"
                )
                region.translation = result or region.text
            except Exception:
                region.translation = region.text  # fallback: keep original
        return regions

    # ── Image rendering ───────────────────────────────────────────

    def render_translated(
        self, image_path: str, regions: list[TextRegion], output_path: str
    ):
        """Paint translated English text over Japanese text regions."""
        img = self.open_image(image_path).convert("RGBA")

        # Create overlay for semi-transparent fills
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Sort by area (largest first) to handle overlapping regions
        sorted_regions = sorted(
            regions,
            key=lambda r: (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]),
            reverse=True,
        )

        for region in sorted_regions:
            if not region.translation:
                continue

            x1, y1, x2, y2 = region.bbox
            box_w = x2 - x1
            box_h = y2 - y1
            if box_w < 10 or box_h < 10:
                continue

            # Sample background color from border around the bbox
            bg_color = self._sample_background(img, region.bbox)

            # Draw filled rectangle to cover original text
            draw.rectangle([x1, y1, x2, y2], fill=bg_color)

            # Choose text color based on background brightness
            text_color = (255, 255, 255) if self._is_dark(bg_color) else (0, 0, 0)
            outline_color = (0, 0, 0) if not self._is_dark(bg_color) else (255, 255, 255)

            # Auto-size font to fit bbox
            font, lines = self._fit_text(region.translation, box_w, box_h)

            # Calculate text position (centered in bbox)
            text_block = "\n".join(lines)
            text_bbox = draw.textbbox((0, 0), text_block, font=font)
            tw = text_bbox[2] - text_bbox[0]
            th = text_bbox[3] - text_bbox[1]
            tx = x1 + (box_w - tw) // 2
            ty = y1 + (box_h - th) // 2

            # Draw text with outline for readability
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((tx + dx, ty + dy), text_block, font=font, fill=outline_color)
            draw.text((tx, ty), text_block, font=font, fill=text_color)

        # Composite overlay onto original
        result = Image.alpha_composite(img, overlay)
        # Save as RGB PNG (strip alpha for game compatibility)
        result = result.convert("RGB")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        result.save(output_path, "PNG")

    @staticmethod
    def _sample_background(img: Image.Image, bbox: tuple, border: int = 4) -> tuple:
        """Sample average color from pixels just outside the bounding box."""
        x1, y1, x2, y2 = bbox
        w, h = img.size
        pixels = []

        # Convert to RGB if needed
        rgb = img.convert("RGB")

        # Sample border pixels outside the bbox
        for x in range(max(0, x1 - border), min(w, x2 + border)):
            for y_off in [max(0, y1 - border), min(h - 1, y2 + border - 1)]:
                if 0 <= x < w and 0 <= y_off < h:
                    pixels.append(rgb.getpixel((x, y_off)))
        for y in range(max(0, y1 - border), min(h, y2 + border)):
            for x_off in [max(0, x1 - border), min(w - 1, x2 + border - 1)]:
                if 0 <= x_off < w and 0 <= y < h:
                    pixels.append(rgb.getpixel((x_off, y)))

        if not pixels:
            return (40, 40, 40, 255)  # dark fallback

        # Median color (more robust than mean for gradients)
        pixels.sort(key=lambda p: sum(p))
        mid = pixels[len(pixels) // 2]
        return (mid[0], mid[1], mid[2], 255)

    @staticmethod
    def _is_dark(color: tuple) -> bool:
        """Check if an RGB(A) color is dark."""
        r, g, b = color[0], color[1], color[2]
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return luminance < 128

    @staticmethod
    def _fit_text(
        text: str, box_w: int, box_h: int, min_size: int = 8
    ) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
        """Find font size and line wrapping that fits text within a bounding box."""
        if not _SYSTEM_FONT:
            # Fallback to Pillow default (no sizing control)
            font = ImageFont.load_default()
            return font, [text]

        # Start large and shrink
        max_size = max(min_size, int(box_h * 0.8))
        dummy = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(dummy)

        for size in range(max_size, min_size - 1, -1):
            font = ImageFont.truetype(_SYSTEM_FONT, size)
            lines = _wrap_text(draw, text, font, box_w - 4)  # 2px padding each side
            block = "\n".join(lines)
            bb = draw.textbbox((0, 0), block, font=font)
            if (bb[2] - bb[0]) <= box_w and (bb[3] - bb[1]) <= box_h:
                return font, lines

        # If nothing fits, use minimum size
        font = ImageFont.truetype(_SYSTEM_FONT, min_size)
        lines = _wrap_text(draw, text, font, box_w - 4)
        return font, lines


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: int
) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    if not words:
        return [text]

    lines = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word
        bb = draw.textbbox((0, 0), test, font=font)
        if (bb[2] - bb[0]) <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines

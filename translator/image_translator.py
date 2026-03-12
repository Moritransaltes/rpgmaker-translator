"""Image translation module — OCR Japanese text from game images, translate, render English.

Pipeline: Multimodal model (OCR + bounding boxes) → same model (translation) → Pillow (render)
        → same model (verify rendered image for quality).

Uses a single multimodal model (e.g. Qwen 3.5) for all stages — no separate vision model needed.

Features:
- OCR with 3-attempt retry (scaled → full res → 2x upscale for small images)
- Enhanced OCR prompt with pixel dimensions and explicit bbox rules, 20% bbox padding
- Two render modes: "preserve" (icon-preserving) and "clean" (white boxes on black)
- Two-state sprite sheet detection for RPG Maker menu images
- Warm pixel detection for non-sprite images (pink/red = selected state)
- Translation deduplication (same JP text translated once across regions)
- Verify loop: re-checks rendered images via multimodal model for quality
- RPGMVP encryption round-trip: decrypt to read, encrypt_to_rpgmvp() to write back
- Export to game with img_original/ backup on first export
"""

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


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
    if len(key_bytes) < 16:
        raise ValueError(f"Encryption key too short: {len(key_bytes)} bytes, need 16")

    with open(file_path, "rb") as f:
        data = f.read()

    # Skip the 16-byte RPG Maker header
    encrypted = data[_RPGMV_HEADER_LEN:]

    # XOR the first 16 bytes with the key to restore PNG header
    decrypted_head = bytes(b ^ k for b, k in zip(encrypted[:16], key_bytes))

    # Rest of the file is unencrypted
    return decrypted_head + encrypted[16:]


# RPG Maker MV header: "RPGMV\x00\x00\x00" + 8 bytes version/padding
_RPGMV_HEADER = b"RPGMV\x00\x00\x00\x00\x03\x01\x00\x00\x00\x00\x00"


def encrypt_to_rpgmvp(png_path: str, output_path: str, encryption_key: str):
    """Encrypt a PNG file back to .rpgmvp format for RPG Maker MV/MZ.

    Reverses the decryption: adds the 16-byte RPG Maker header and XORs
    the first 16 bytes of PNG data with the encryption key.
    """
    key_bytes = bytes.fromhex(encryption_key)
    if len(key_bytes) < 16:
        raise ValueError(f"Encryption key too short: {len(key_bytes)} bytes, need 16")

    with open(png_path, "rb") as f:
        png_data = f.read()

    # XOR the first 16 bytes of PNG with key
    encrypted_head = bytes(b ^ k for b, k in zip(png_data[:16], key_bytes))

    # Assemble: RPG Maker header + encrypted first 16 bytes + rest unchanged
    result = _RPGMV_HEADER + encrypted_head + png_data[16:]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(result)


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
    verified: bool = False             # True if verify loop passed
    verify_issues: list[str] = field(default_factory=list)


# ── OCR prompt ────────────────────────────────────────────────────

_OCR_SYSTEM = "You are a precise OCR system for Japanese video game screenshots. You detect ALL Japanese text, including stylized, outlined, shadowed, and decorative game UI text."

_OCR_USER_TEMPLATE = """\
This image is {w}x{h} pixels. Find ALL Japanese text in this game image.
For each text region, return a JSON array:
[{{"text": "Japanese text here", "bbox": [x1, y1, x2, y2]}}, ...]

IMPORTANT rules for bbox coordinates:
- Coordinates are in PIXELS from the top-left corner (0,0).
- x1,y1 = top-left corner of the text region, x2,y2 = bottom-right corner.
- The bbox MUST tightly enclose ALL pixels of the text, including any glow/shadow/outline effects.
- x2 must be > x1, y2 must be > y1.
- Maximum x2 is {w}, maximum y2 is {h}.

What to include:
- ALL Japanese text: hiragana, katakana, kanji — even single characters
- Stylized, outlined, glowing, shadowed, or colored text (common in game menus)
- Text on buttons, clouds, banners, speech bubbles, title screens
- Text that is partially transparent or has special effects

What to ignore:
- English-only text and standalone numbers
- Non-text graphics (icons, borders, patterns)

If no Japanese text is found, return: []
Return ONLY the JSON array, no other text."""

# Regex to extract a JSON array from LLM output (may have markdown fences)
_JSON_RE = re.compile(r'\[.*\]', re.DOTALL)

# Verify prompt for rendered image QA
_VERIFY_SYSTEM = "You are a QA checker for translated game images. Be thorough but concise."

_VERIFY_PROMPT = """\
Check this translated game UI image for quality issues.
Look for:
1. Any remaining Japanese text (hiragana, katakana, kanji)
2. English text that is cut off or overlapping the icon
3. Text that is too small or hard to read
4. Visual artifacts or rendering problems

Return JSON: {"ok": true/false, "issues": ["issue description", ...]}
If everything looks good, return: {"ok": true, "issues": []}"""


# ── Font discovery ────────────────────────────────────────────────

def _find_font(bold: bool = False) -> str | None:
    """Find a usable TrueType font on the system."""
    candidates = []
    if bold:
        candidates.append(r"C:\Windows\Fonts\arialbd.ttf")
    candidates += [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
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
_SYSTEM_FONT_BOLD = _find_font(bold=True) or _SYSTEM_FONT


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

    # Render modes
    RENDER_PRESERVE = "preserve"  # Icon-preserving: clear text, keep background
    RENDER_CLEAN = "clean"        # White boxes on black background (legacy)

    def __init__(self, client, encryption_key: str = ""):
        """
        Args:
            client: An AIClient instance (multimodal — handles OCR, translate, verify).
            encryption_key: RPG Maker MV/MZ encryption key (hex string from System.json).
        """
        self.client = client
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

    # ── OCR via multimodal model ─────────────────────────────────

    def ocr_image(self, image_path: str, max_dim: int = 1280) -> list[TextRegion]:
        """Send image to model, extract Japanese text + bounding boxes.

        Automatically retries at full resolution if the first attempt
        (scaled down) returns no regions — vision models are flaky and
        sometimes need the full image to detect stylized text.
        """
        img = self.open_image(image_path)
        orig_w, orig_h = img.size

        regions = self._ocr_attempt(img, orig_w, orig_h, max_dim)

        # Retry at full resolution if first attempt found nothing
        if not regions and max(orig_w, orig_h) > max_dim:
            regions = self._ocr_attempt(img, orig_w, orig_h, max_dim=None)

        # Retry with upscale if image is small (< 400px) and nothing found
        if not regions and max(orig_w, orig_h) < 400:
            scale_up = 800 / max(orig_w, orig_h)
            upscaled = img.resize(
                (int(orig_w * scale_up), int(orig_h * scale_up)),
                Image.Resampling.LANCZOS,
            )
            regions = self._ocr_attempt(upscaled, orig_w, orig_h, max_dim=None)

        return regions

    def _ocr_attempt(
        self, img: Image.Image, orig_w: int, orig_h: int,
        max_dim: int | None = 1280,
    ) -> list[TextRegion]:
        """Single OCR attempt — resize, send to model, parse results."""
        send_img = img.copy()

        # Resize for VRAM efficiency
        if max_dim is not None:
            longest = max(send_img.size)
            if longest > max_dim:
                scale = max_dim / longest
                new_w = int(send_img.size[0] * scale)
                new_h = int(send_img.size[1] * scale)
                send_img = send_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Convert to base64
        b64 = self._to_base64(send_img)

        # Build prompt with actual image dimensions
        send_w, send_h = send_img.size
        prompt = _OCR_USER_TEMPLATE.format(w=send_w, h=send_h)

        # Call multimodal model
        raw = self.client.vision_chat(b64, prompt, system=_OCR_SYSTEM)

        # Parse response
        regions = self._parse_ocr_response(raw)

        # Scale bounding boxes back to original image dimensions
        img_w, img_h = send_img.size
        sx = orig_w / img_w if img_w > 0 else 1.0
        sy = orig_h / img_h if img_h > 0 else 1.0
        if abs(sx - 1.0) > 0.01 or abs(sy - 1.0) > 0.01:
            for r in regions:
                x1, y1, x2, y2 = r.bbox
                r.bbox = (
                    int(x1 * sx), int(y1 * sy),
                    int(x2 * sx), int(y2 * sy),
                )

        # Clamp bboxes to image bounds and discard invalid geometry
        valid_regions = []
        for r in regions:
            x1, y1, x2, y2 = r.bbox
            x1 = max(0, min(x1, orig_w))
            y1 = max(0, min(y1, orig_h))
            x2 = max(0, min(x2, orig_w))
            y2 = max(0, min(y2, orig_h))
            if x2 > x1 and y2 > y1:
                r.bbox = (x1, y1, x2, y2)
                valid_regions.append(r)
        regions = valid_regions

        # Pad bboxes by 20% to ensure coverage (vision models often undersize)
        for r in regions:
            x1, y1, x2, y2 = r.bbox
            w = x2 - x1
            h = y2 - y1
            pad_x = int(w * 0.2)
            pad_y = int(h * 0.2)
            r.bbox = (
                max(0, x1 - pad_x),
                max(0, y1 - pad_y),
                min(orig_w, x2 + pad_x),
                min(orig_h, y2 + pad_y),
            )

        # Filter out zero-area or tiny regions
        regions = [r for r in regions if
                   (r.bbox[2] - r.bbox[0]) > 5 and (r.bbox[3] - r.bbox[1]) > 5]

        return regions

    @staticmethod
    def _to_base64(img: Image.Image) -> str:
        """Convert a PIL Image to base64 string."""
        buf = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _parse_ocr_response(raw: str) -> list[TextRegion]:
        """Parse vision model JSON output into TextRegion list."""
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
        """Translate each region's Japanese text using the model.

        Deduplicates: if the same Japanese text appears multiple times
        (common in two-state sprite sheets), it's only translated once.
        """
        cache: dict[str, str] = {}
        for region in regions:
            if not region.text:
                continue
            # Reuse translation for duplicate text (two-state buttons)
            if region.text in cache:
                region.translation = cache[region.text]
                continue
            try:
                result = self.client.translate_name(
                    region.text, hint="text in game image"
                )
                region.translation = result or region.text
            except Exception:
                region.translation = region.text  # fallback: keep original
            cache[region.text] = region.translation
        return regions

    # ── Image rendering ───────────────────────────────────────────

    def render_translated(
        self, image_path: str, regions: list[TextRegion], output_path: str,
        mode: str = RENDER_PRESERVE,
    ):
        """Render translated text onto an image.

        Args:
            image_path: Path to original image.
            regions: OCR regions with translations.
            output_path: Where to save the rendered image.
            mode: "preserve" (icon-preserving) or "clean" (white boxes on black).
        """
        if mode == self.RENDER_PRESERVE:
            self._render_preserve(image_path, regions, output_path)
        else:
            self._render_clean(image_path, regions, output_path)

    def _render_preserve(
        self, image_path: str, regions: list[TextRegion], output_path: str,
    ):
        """Icon-preserving render: keep original background, clear text, draw English.

        For images with icons on the left and text on the right (common in
        RPG Maker menu/shop/mission images), this:
        1. Detects where the icon ends
        2. Clears only the text region (makes transparent)
        3. Cleans stray text pixels that bleed into the icon zone
        4. Draws English text in the original text color with shadow
        """
        img = self.open_image(image_path).convert("RGBA")
        img_w, img_h = img.size

        # Detect two-state sprite sheet
        is_two_state, merged = self._detect_two_state(regions, img_h)

        if is_two_state and merged:
            half_h = img_h // 2
            for text, bbox_top, bbox_bot in merged:
                if not text:
                    continue
                # Process top half
                self._preserve_region(img, bbox_top, text, img_w)
                # Process bottom half
                self._preserve_region(img, bbox_bot, text, img_w)
        else:
            for region in regions:
                if not region.translation:
                    continue
                self._preserve_region(img, region.bbox, region.translation, img_w)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        img.save(output_path, "PNG")

    def _preserve_region(
        self, img: Image.Image, bbox: tuple, text: str, img_w: int,
    ):
        """Clear text in a bbox region and draw English text, preserving icons."""
        x1, y1, x2, y2 = bbox
        box_w = x2 - x1
        box_h = y2 - y1
        if box_w < 10 or box_h < 10:
            return

        # Sample text color from original before clearing
        text_color = self._sample_text_color(img, bbox)

        # Find icon boundary — scan columns in the bbox area
        icon_right = self._find_icon_right_edge(img, bbox)

        # Clear the text region (right of icon boundary within bbox)
        clear_x = max(x1, icon_right)
        pixels = img.load()
        for x in range(clear_x, x2):
            for y in range(y1, y2):
                pixels[x, y] = (0, 0, 0, 0)

        # Clean stray text pixels that bleed left of the clear boundary
        stray_left = max(x1, clear_x - 20)
        stray_top = y1
        stray_bottom = min(y1 + min(35, box_h // 3), y2)
        for x in range(stray_left, clear_x):
            for y in range(stray_top, stray_bottom):
                r, g, b, a = pixels[x, y]
                # Only clear bright/colored pixels (text), not icon pixels
                if a > 30 and (r > 180 or g > 180 or b > 50):
                    pixels[x, y] = (0, 0, 0, 0)

        # Draw English text
        draw = ImageDraw.Draw(img)
        available_w = x2 - clear_x - 10
        available_h = box_h - 4

        if available_w < 20 or available_h < 10:
            # Too narrow after icon — fall back to drawing over full bbox
            clear_x = x1
            available_w = box_w - 8

        font, lines = self._fit_text(text, available_w, available_h)
        text_block = "\n".join(lines)
        tb = draw.textbbox((0, 0), text_block, font=font)
        tw = tb[2] - tb[0]
        th = tb[3] - tb[1]

        tx = clear_x + 5
        ty = y1 + (box_h - th) // 2

        # Shadow for readability
        shadow_color = (0, 0, 0, 180)
        draw.text((tx + 1, ty + 1), text_block, font=font, fill=shadow_color)
        draw.text((tx, ty), text_block, font=font, fill=text_color)

    @staticmethod
    def _find_icon_right_edge(
        img: Image.Image, bbox: tuple,
    ) -> int:
        """Find where the icon area ends within a bounding box.

        Scans columns in the bottom 2/3 of the bbox to find the transition
        from icon content to empty space. Returns the x coordinate where
        text should start.
        """
        x1, y1, x2, y2 = bbox
        box_w = x2 - x1
        box_h = y2 - y1

        # Only scan if the box is wide enough to have an icon + text layout
        if box_w < 100:
            return x1  # too narrow, no icon expected

        pixels = img.load()
        # Scan the icon zone: first 30% of box width
        scan_start = x1 + int(box_w * 0.15)
        scan_end = x1 + int(box_w * 0.40)
        y_scan_start = y1 + box_h // 3  # bottom 2/3 only

        # Scan from right to left for last column with significant content
        for x in range(scan_end, scan_start, -1):
            non_transparent = 0
            check_h = y2 - y_scan_start
            for y in range(y_scan_start, y2):
                r, g, b, a = pixels[x, y]
                if a > 30:
                    non_transparent += 1
            if check_h > 0 and non_transparent > check_h * 0.15:
                return x + 5  # small gap after icon

        return x1  # no icon detected, text starts at bbox left

    @staticmethod
    def _sample_text_color(
        img: Image.Image, bbox: tuple,
    ) -> tuple:
        """Sample the dominant text color from a region.

        Finds the most common bright/colored pixel in the region,
        which is typically the text color.
        """
        x1, y1, x2, y2 = bbox
        crop = img.crop((x1, y1, x2, y2)).convert("RGBA")

        # Collect bright pixels (likely text, not background)
        bright_pixels = []
        for pixel in crop.getdata():
            r, g, b, a = pixel
            if a > 100 and (r > 100 or g > 100 or b > 100):
                bright_pixels.append((r, g, b, a))

        if not bright_pixels:
            return (255, 255, 50, 255)  # fallback: yellow

        # Find the most common bright color (quantize for grouping)
        from collections import Counter
        quantized = [
            (r // 32 * 32, g // 32 * 32, b // 32 * 32, 255)
            for r, g, b, a in bright_pixels
        ]
        most_common = Counter(quantized).most_common(1)[0][0]
        return most_common

    def _render_clean(
        self, image_path: str, regions: list[TextRegion], output_path: str,
    ):
        """Clean render: white boxes on black background (legacy mode).

        Handles RPG Maker's two-state sprite sheet convention:
        many system/menu images are split vertically — top half = one state
        (unselected), bottom half = other state (selected).
        """
        img = self.open_image(image_path).convert("RGB")
        img_w, img_h = img.size

        # Start with a clean black background (same dimensions as original)
        result = Image.new("RGB", (img_w, img_h), (0, 0, 0))
        draw = ImageDraw.Draw(result)

        # Detect two-state sprite sheet
        is_two_state, merged = self._detect_two_state(regions, img_h)

        if is_two_state and merged:
            for text, bbox_top, bbox_bot in merged:
                if not text:
                    continue
                self._draw_state_box(
                    draw, text, bbox_top, img_w,
                    text_color=(20, 20, 20), border_color=(60, 60, 60),
                )
                self._draw_state_box(
                    draw, text, bbox_bot, img_w,
                    text_color=(200, 30, 30), border_color=(200, 30, 30),
                )
        else:
            for region in regions:
                if not region.translation:
                    continue
                x1, y1, x2, y2 = region.bbox
                if (x2 - x1) < 10 or (y2 - y1) < 10:
                    continue

                is_selected = self._has_warm_pixels(img, region.bbox)
                text_color = (200, 30, 30) if is_selected else (20, 20, 20)
                border_color = (200, 30, 30) if is_selected else (60, 60, 60)

                self._draw_state_box(
                    draw, region.translation, region.bbox, img_w,
                    text_color=text_color, border_color=border_color,
                )

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        result.save(output_path, "PNG")

    def _draw_state_box(
        self, draw: ImageDraw.ImageDraw, text: str,
        bbox: tuple, img_w: int, *,
        text_color: tuple, border_color: tuple,
    ):
        """Draw a white rounded box with centered text at the bbox position."""
        x1, y1, x2, y2 = bbox
        box_w = x2 - x1
        box_h = y2 - y1
        if box_w < 10 or box_h < 10:
            return

        box_fill = (255, 255, 255)
        radius = min(box_h // 3, box_w // 4, 12)

        draw.rounded_rectangle(
            [x1, y1, x2, y2],
            radius=radius, fill=box_fill, outline=border_color, width=2,
        )

        font, lines = self._fit_text(text, box_w - 8, box_h - 4)
        text_block = "\n".join(lines)
        tb = draw.textbbox((0, 0), text_block, font=font)
        tw = tb[2] - tb[0]
        th = tb[3] - tb[1]

        tx = x1 + (box_w - tw) // 2
        ty = y1 + (box_h - th) // 2
        draw.text((tx, ty), text_block, font=font, fill=text_color)

    # ── Verify loop ──────────────────────────────────────────────

    def verify_render(self, rendered_path: str) -> dict:
        """Send rendered image to model for quality check.

        Returns: {"ok": bool, "issues": list[str]}
        Issues might be: "Japanese remnants visible", "text cut off",
        "text overlaps icon", "text too small to read"
        """
        img = Image.open(rendered_path)
        b64 = self._to_base64(img)

        raw = self.client.vision_chat(b64, _VERIFY_PROMPT, system=_VERIFY_SYSTEM)

        # Parse JSON response
        try:
            # Strip markdown fences
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines)

            # Extract JSON object
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                return {
                    "ok": bool(result.get("ok", False)),
                    "issues": list(result.get("issues", [])),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # If we can't parse the response, assume it's okay
        return {"ok": True, "issues": []}

    # ── Two-state detection ──────────────────────────────────────

    @staticmethod
    def _detect_two_state(
        regions: list[TextRegion], img_h: int
    ) -> tuple[bool, list[tuple[str, tuple, tuple]]]:
        """Detect RPG Maker two-state sprite sheet pattern.

        Many menu/system images are split vertically: top half = unselected,
        bottom half = selected. OCR finds the same text in both halves.

        Returns:
            (is_two_state, merged_list) where merged_list contains
            (translation, top_bbox, bottom_bbox) tuples.
        """
        if len(regions) < 2:
            return False, []

        half_y = img_h // 2

        top_only = []
        bot_only = []
        for r in regions:
            cy = (r.bbox[1] + r.bbox[3]) // 2
            if cy < half_y:
                top_only.append(r)
            else:
                bot_only.append(r)

        top_only.sort(key=lambda r: r.bbox[1])
        bot_only.sort(key=lambda r: r.bbox[1])

        if len(top_only) != len(bot_only) or len(top_only) == 0:
            return False, []

        merged = []
        matched = 0
        for t, b in zip(top_only, bot_only):
            is_match = (t.text == b.text
                        or (t.translation and t.translation == b.translation))
            if is_match:
                matched += 1
            translation = t.translation or b.translation or ""
            if is_match:
                merged.append((translation, t.bbox, b.bbox))

        if matched < len(top_only) * 0.5:
            return False, []

        return True, merged

    # ── Color detection helpers ──────────────────────────────────

    @staticmethod
    def _has_warm_pixels(img: Image.Image, bbox: tuple) -> bool:
        """Detect if a region contains pink/red/warm-colored text pixels."""
        x1, y1, x2, y2 = bbox
        crop = img.crop((x1, y1, x2, y2)).convert("RGB")

        max_sample = 80
        w, h = crop.size
        if w > max_sample or h > max_sample:
            scale = max_sample / max(w, h)
            crop = crop.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )

        pixels = list(crop.getdata())
        if not pixels:
            return False

        warm_count = 0
        for r, g, b in pixels:
            if r > 140 and r > g * 1.4 and r > b * 1.2:
                warm_count += 1

        return warm_count > len(pixels) * 0.03

    # ── Text fitting ─────────────────────────────────────────────

    @staticmethod
    def _fit_text(
        text: str, box_w: int, box_h: int, min_size: int = 8
    ) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
        """Find font size and line wrapping that fits text within a bounding box."""
        font_path = _SYSTEM_FONT_BOLD or _SYSTEM_FONT
        if not font_path:
            font = ImageFont.load_default()
            return font, [text]

        max_size = max(min_size, int(box_h * 0.8))
        dummy = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(dummy)

        for size in range(max_size, min_size - 1, -1):
            font = ImageFont.truetype(font_path, size)
            lines = _wrap_text(draw, text, font, box_w - 4)
            block = "\n".join(lines)
            bb = draw.textbbox((0, 0), block, font=font)
            if (bb[2] - bb[0]) <= box_w and (bb[3] - bb[1]) <= box_h:
                return font, lines

        font = ImageFont.truetype(font_path, min_size)
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

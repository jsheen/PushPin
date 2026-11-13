"""Draw pushPin's red pushpin icon at every size it's used.

    uv run --no-project --with pillow python scripts/make_icons.py

Writes the web icons (served by the card server) to src/pushpin/assets/ and the iOS app icon to
ios/PushPin/Assets.xcassets/AppIcon.appiconset/. Drawn at 4x and scaled down for smooth edges.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RED, DARK, NEEDLE, SHINE, CREAM = "#EF3B4A", "#C8202F", "#9AA0A8", "#FFFFFF", "#FFF4EE"


def pin(size: int, background: str | None = None, scale: float = 1.0) -> Image.Image:
    """A round red pin head on a little collar, with a short silver needle, tilted as if just pushed in."""
    s = size * 4
    img = Image.new("RGBA", (s, s), background or (0, 0, 0, 0))
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    u = s / 64 * scale  # design units: a 64-unit square, centered
    ox = oy = s / 2 - 32 * u

    def box(x0, y0, x1, y1):
        return [ox + x0 * u, oy + y0 * u, ox + x1 * u, oy + y1 * u]

    d.line([ox + 32 * u, oy + 44 * u, ox + 32 * u, oy + 60 * u], fill=NEEDLE, width=round(3.2 * u))
    d.ellipse(box(30.4, 58.4, 33.6, 61.6), fill=NEEDLE)                 # rounded needle tip
    d.rounded_rectangle(box(20, 39, 44, 46), radius=3.5 * u, fill=DARK)  # collar
    d.rectangle(box(27, 31, 37, 41), fill=DARK)                          # neck
    d.ellipse(box(14, 4, 50, 40), fill=RED)                              # head
    d.ellipse(box(21, 10, 31, 18), fill=SHINE + "B3")                    # shine
    layer = layer.rotate(-28, resample=Image.BICUBIC, center=(s / 2, s / 2))
    x0, y0, x1, y1 = layer.getbbox()  # center what's drawn, not the design square
    img.alpha_composite(layer, (round(s / 2 - (x0 + x1) / 2), round(s / 2 - (y0 + y1) / 2)))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    assets = ROOT / "src" / "pushpin" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    pin(64).save(assets / "favicon.png")
    pin(180, CREAM, scale=0.82).save(assets / "apple-touch-icon.png")   # home screen: iOS adds the rounded corners
    pin(192).save(assets / "icon.png")                                  # ntfy's notification icon (Android)
    app = ROOT / "ios" / "PushPin" / "Assets.xcassets" / "AppIcon.appiconset"
    pin(1024, CREAM, scale=0.82).convert("RGB").save(app / "AppIcon.png")  # App Store icons have no transparency
    print("icons written")


if __name__ == "__main__":
    main()

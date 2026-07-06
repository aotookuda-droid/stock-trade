#!/usr/bin/env python3
"""グラビア誌表紙をJSON設定からPNGとして書き出すジェネレータ。

使い方:
    python generate.py config.sample.json

出力はB5判 (182x257mm) を300dpiでレンダリングした 2150x3035px のPNG。
日本語フォントは config の font_path で指定するか、
Noto Sans CJK 等のシステムフォントを自動検出する。
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# B5判 182x257mm @ 300dpi
WIDTH, HEIGHT = 2150, 3035

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]


def find_font(config_path: str | None) -> str:
    candidates = ([config_path] if config_path else []) + FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path
    sys.exit(
        "日本語フォントが見つかりません。config の font_path に "
        "TTF/TTC/OTF のパスを指定してください。"
    )


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def draw_text_with_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0, 160), offset=8):
    x, y = xy
    draw.multiline_text((x + offset, y + offset), text, font=font, fill=shadow)
    draw.multiline_text((x, y), text, font=font, fill=fill)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"使い方: python {Path(__file__).name} <config.json>")

    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    font_path = find_font(config.get("font_path"))

    accent = config.get("accent_color", "#e6007e")
    sub_color = config.get("sub_color", "#ffe600")

    canvas = Image.new("RGB", (WIDTH, HEIGHT), "#d8d3cc")

    # --- 表紙写真 ---
    photo_path = config.get("photo")
    if photo_path and Path(photo_path).exists():
        photo = Image.open(photo_path).convert("RGB")
        scale = max(WIDTH / photo.width, HEIGHT / photo.height)
        photo = photo.resize((round(photo.width * scale), round(photo.height * scale)))
        canvas.paste(photo, ((WIDTH - photo.width) // 2, 0))

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # --- 誌名ロゴ ---
    logo_font = load_font(font_path, 340)
    draw.text(
        (110, 70), config["logo"], font=logo_font, fill=accent,
        stroke_width=12, stroke_fill="white",
    )

    # --- 号数帯 ---
    issue_font = load_font(font_path, 64)
    issue = config["issue"]
    box = draw.textbbox((0, 0), issue, font=issue_font)
    draw.rectangle((120, 470, 120 + (box[2] - box[0]) + 60, 470 + (box[3] - box[1]) + 50), fill="#111111")
    draw.text((150, 485), issue, font=issue_font, fill="white")

    # --- サブ見出し（右側縦積み） ---
    sub_font = load_font(font_path, 58)
    y = 680
    for line in config.get("sublines", []):
        box = draw.textbbox((0, 0), line, font=sub_font)
        w = box[2] - box[0]
        x = WIDTH - w - 140
        draw.rectangle((x - 40, y - 16, WIDTH - 90, y + (box[3] - box[1]) + 30), fill=(255, 255, 255, 235))
        draw.rectangle((x - 40, y - 16, x - 18, y + (box[3] - box[1]) + 30), fill=accent)
        draw.text((x, y), line, font=sub_font, fill="#111111")
        y += (box[3] - box[1]) + 90

    # --- メイン特集 ---
    main_font = load_font(font_path, 180)
    main_copy = config["main_copy"]
    box = draw.multiline_textbbox((0, 0), main_copy, font=main_font)
    main_y = HEIGHT - 600 - (box[3] - box[1])
    draw_text_with_shadow(draw, (120, main_y), main_copy, main_font, "white")

    # --- モデル名 ---
    model_font = load_font(font_path, 100)
    draw_text_with_shadow(draw, (120, HEIGHT - 500), config["model_name"], model_font, sub_color)

    # --- 価格・バーコード帯 ---
    band_top = HEIGHT - 220
    draw.rectangle((0, band_top, WIDTH, HEIGHT), fill=(255, 255, 255, 245))
    price_font = load_font(font_path, 56)
    draw.text((80, band_top + 70), config["price"], font=price_font, fill="#111111")
    x = WIDTH - 560
    while x < WIDTH - 100:
        bar = 6 if (x // 10) % 3 else 12
        draw.rectangle((x, band_top + 45, x + bar, HEIGHT - 45), fill="#111111")
        x += bar + 10

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    output = config.get("output", "cover.png")
    canvas.save(output, dpi=(300, 300))
    print(f"書き出し完了: {output} ({WIDTH}x{HEIGHT}px, 300dpi)")


if __name__ == "__main__":
    main()

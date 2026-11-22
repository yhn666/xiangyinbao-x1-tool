"""标定卡：200 点正方形 + 横竖标尺（1 点 = 1/203 英寸，8 点 = 1mm）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont

W = 384
H = 300
M = 12
MM = 8  # 203dpi 下 1mm ≈ 8 点


def build() -> Image.Image:
    f = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 20)
    fs = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)

    d.text((M, 4), "标定卡：方形 200 点 / 标尺 48mm", font=f, fill=0)

    # 横标尺（顶部）
    y0 = 34
    for mm in range(49):
        x = M + mm * MM
        if x > W - M:
            break
        long = mm % 5 == 0
        d.line([x, y0, x, y0 + (12 if long else 6)], fill=0, width=1)
    d.line([M, y0, W - M, y0], fill=0, width=1)
    for mm in (0, 10, 20, 30, 40, 48):
        x = M + mm * MM
        if x <= W - M:
            d.text((x - 6, y0 + 14), str(mm), font=fs, fill=0)

    # 200 点正方形
    sq = 200
    sx, sy = M, y0 + 40
    d.rectangle([sx, sy, sx + sq - 1, sy + sq - 1], outline=0, width=4)
    d.text((sx + 8, sy + 8), "200 点", font=fs, fill=0)
    d.text((sx + 8, sy + 24), "应为 25mm", font=fs, fill=0)

    # 竖标尺（正方形右侧）30mm
    rx = sx + sq + 16
    for mm in range(31):
        y = sy + mm * MM
        long = mm % 5 == 0
        d.line([rx, y, rx + (12 if long else 6), y], fill=0, width=1)
    d.line([rx, sy, rx, sy + 30 * MM], fill=0, width=1)
    for mm in (0, 10, 20, 30):
        d.text((rx + 14, sy + mm * MM - 6), str(mm), font=fs, fill=0)

    # 竖直灰阶条：与正方形等高，看有无拉伸
    gx = sx + 60
    for i, shade in enumerate((0, 90, 170, 230)):
        d.rectangle([gx + i * 22, sy + sq - 40, gx + i * 22 + 20, sy + sq - 1], fill=shade)

    return img


if __name__ == "__main__":
    img = build()
    img.save("_calib.png")
    print("尺寸:", img.size)


"""纸尾标定条：一条基准线 + 10 行（30mm）走纸，用来量实际空白距离。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont

W, H = 384, 70


def build() -> Image.Image:
    f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.text((16, 6), "纸尾标定：底线以上是正文", font=f, fill=0)
    d.rectangle([16, H - 12, W - 16, H - 4], fill=0)  # 基准线（粗 8 点 = 1mm）
    return img


if __name__ == "__main__":
    img = build()
    img.save("_tail.png")
    print("尺寸:", img.size)


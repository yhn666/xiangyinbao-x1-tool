"""轻量布局 DSL：用 YAML 描述一页 384 点的票据/便签，纯 Pillow 渲染。

参考 go-go-golems/almanach 的 block 思路，但只依赖 Pillow + PyYAML，不带 Chrome/Go。

示例：
    theme: minimal
    blocks:
      - type: title
        text: 每日便签
        subtitle: 2026-11-22 星期六
      - type: divider
        style: double
      - type: kv
        items:
          - [浓度, 95]
          - [纸尾, 30mm]
      - type: text
        text: 今天要做的三件事
      - type: qr
        data: https://example.com
        caption: 扫码查看
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from yaml import safe_load

from x1 import load_font

W = 384

THEMES = {
    "minimal": {"frame": None, "title": "bold", "body": "sans", "rule": 1},
    "classic": {"frame": "double", "title": "serif", "body": "serif", "rule": 2},
    "notebook": {"frame": None, "title": "bold", "body": "sans", "rule": 1, "ruled": True},
}


def _font(kind: str, size: int) -> ImageFont.ImageFont:
    return load_font(size, kind)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw:
            out.append("")
            continue
        cur = ""
        for ch in raw:
            if font.getlength(cur + ch) <= width:
                cur += ch
            else:
                out.append(cur)
                cur = ch
        out.append(cur)
    return out


class LayoutRenderer:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.theme_name = spec.get("theme", "minimal")
        self.theme = THEMES.get(self.theme_name, THEMES["minimal"])
        self.margin = int(spec.get("margin", 14))          # 左边距
        self.mright = int(spec.get("margin_right", self.margin))   # 右边距，默认与左对称
        self.body = int(spec.get("bodyScale", 20))
        self.blocks = spec.get("blocks", [])
        self.images: list[tuple[Image.Image, int]] = []   # (块图, y)
        self.height = 0

    # ---------- 各 block 的渲染 ----------
    def _render_block(self, b: dict) -> Image.Image:
        kind = b.get("type", "text")
        inner_w = W - self.margin - self.mright

        if kind == "title":
            size = int(b.get("size", self.body + 8))
            f = _font(self.theme["title"], size)
            f2 = _font(self.theme["body"], max(14, size - 10))
            lines = _wrap(str(b.get("text", "")), f, inner_w)
            sub = b.get("subtitle")
            h = len(lines) * int(size * 1.3) + (int(size * 0.95) if sub else 0) + 6
            img = Image.new("L", (W, h), 255)
            d = ImageDraw.Draw(img)
            y = 0
            for line in lines:
                d.text((self.margin, y), line, font=f, fill=0)
                y += int(size * 1.3)
            if sub:
                d.text((self.margin, y), str(sub), font=f2, fill=90)
            return img

        if kind == "divider":
            style = b.get("style", "line")
            h = 14
            img = Image.new("L", (W, h), 255)
            d = ImageDraw.Draw(img)
            y = h // 2
            x0, x1 = self.margin, W - self.mright
            if style == "dots":
                for x in range(x0, x1, 8):
                    d.rectangle([x, y - 1, x + 3, y + 1], fill=0)
            elif style == "double":
                d.line([x0, y - 3, x1, y - 3], fill=0, width=1)
                d.line([x0, y + 3, x1, y + 3], fill=0, width=1)
            elif style == "wave":
                prev = None
                for x in range(x0, x1):
                    yy = y + (2 if (x // 4) % 2 else -2)
                    if prev:
                        d.line([prev[0], prev[1], x, yy], fill=0, width=1)
                    prev = (x, yy)
            else:
                d.line([x0, y, x1, y], fill=0, width=self.theme["rule"])
            return img

        if kind == "kv":
            size = int(b.get("size", self.body))
            f = _font(self.theme["body"], size)
            items = b.get("items", [])
            h = len(items) * int(size * 1.45) + 4
            img = Image.new("L", (W, h), 255)
            d = ImageDraw.Draw(img)
            y = 2
            for item in items:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    k, v = str(item[0]), str(item[1])
                else:
                    k, v = str(item), ""
                d.text((self.margin, y), k, font=f, fill=0)
                vw = d.textlength(v, font=f)
                d.text((W - self.mright - vw, y), v, font=f, fill=0)
                kw = d.textlength(k, font=f)
                # 点状引导线
                dot_y = y + int(size * 0.75)
                x = self.margin + kw + 6
                limit = W - self.mright - vw - 6
                while x < limit:
                    d.point((x, dot_y), fill=0)
                    x += 6
                y += int(size * 1.45)
            return img

        if kind == "text":
            size = int(b.get("size", self.body))
            f = _font(self.theme["body"], size)
            lines = _wrap(str(b.get("text", "")), f, inner_w)
            lh = int(size * 1.35)
            img = Image.new("L", (W, len(lines) * lh + 8), 255)
            d = ImageDraw.Draw(img)
            align = b.get("align", "left")
            y = 4
            for line in lines:
                tw = d.textlength(line, font=f)
                x = self.margin if align == "left" else (W - tw) // 2
                d.text((x, y), line, font=f, fill=0)
                y += lh
            return img

        if kind == "qr":
            from x1 import make_qr
            return make_qr(str(b.get("data", "")), b.get("caption"))

        if kind == "barcode":
            from x1 import make_barcode
            return make_barcode(str(b.get("data", "")), b.get("format", "code128"),
                                b.get("caption"))

        if kind == "image":
            from PIL import ImageOps
            src = b.get("src")
            if not src or not Path(src).is_file():
                img = Image.new("L", (W, 60), 255)
                ImageDraw.Draw(img).text((self.margin, 16), f"[缺图片] {src}", fill=0)
                return img
            im = Image.open(src).convert("L")
            target_h = int(b.get("height", 180))
            if b.get("fit", "cover") == "cover":
                scale = max((W - self.margin - self.mright) / im.width, target_h / im.height)
            else:
                scale = min((W - self.margin - self.mright) / im.width, target_h / im.height)
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale)),
                           ), Image.Resampling.LANCZOS)
            canvas = Image.new("L", (W - 2 * self.margin, target_h), 255)
            canvas.paste(im, ((canvas.width - im.width) // 2, (canvas.height - im.height) // 2))
            pad = 4 if b.get("border") else 0
            out = Image.new("L", (canvas.width + pad * 2, canvas.height + pad * 2), 255)
            out.paste(canvas, (pad, pad))
            if b.get("border"):
                ImageDraw.Draw(out).rectangle([0, 0, out.width - 1, out.height - 1],
                                              outline=0, width=2)
            framed = Image.new("L", (W, out.height), 255)
            framed.paste(out, (self.margin, 0))
            return framed

        if kind == "space":
            mm = float(b.get("mm", 3))
            return Image.new("L", (W, max(2, int(mm * 8))), 255)

        # 未知块：按 text 处理，便于排错
        return self._render_block({"type": "text", "text": str(b)})

    # ---------- 组装 ----------
    def render(self) -> Image.Image:
        gap = int(self.spec.get("gap", 10))
        parts = [self._render_block(b) for b in self.blocks]
        frame_w = 3 if self.theme["frame"] else 0
        pad = self.margin + frame_w + 4
        body_h = sum(p.height for p in parts) + gap * max(0, len(parts) - 1)
        total_h = body_h + pad * 2
        page = Image.new("L", (W, total_h), 255)
        d = ImageDraw.Draw(page)

        if self.theme.get("ruled"):
            for y in range(pad + 6, total_h - pad, 30):
                for x in range(self.margin, W - self.mright, 4):
                    d.point((x, y), fill=235)

        y = pad
        for p in parts:
            page.paste(p, (0, y))   # 各 block 已自带左右边距，别再二次偏移
            y += p.height + gap

        if self.theme["frame"] == "double":
            d.rectangle([frame_w, frame_w, W - 1 - frame_w, total_h - 1 - frame_w],
                        outline=0, width=2)
            inset = frame_w + 4
            d.rectangle([inset, inset, W - 1 - inset, total_h - 1 - inset],
                        outline=0, width=1)
        return page


def render_layout(path: str | Path) -> Image.Image:
    spec = safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("布局文件顶层必须是字典")
    return LayoutRenderer(spec).render()

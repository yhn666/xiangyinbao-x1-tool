"""不依赖打印机的单元测试：帧格式、位序、偏移、布局渲染。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

import x1


def test_frame_layout_and_crc():
    frame = x1.build_frame(cmd=1, pkg=0, payload=b"", key=x1.DEFAULT_CRC_KEY)
    assert frame[0] == x1.STX and frame[-1] == x1.ETX
    assert frame[1] == 1 and frame[2] == 0
    assert int.from_bytes(frame[3:5], "little") == 0
    # 空 payload 的 CRC 就等于初值本身
    assert frame[5:9] == x1.DEFAULT_CRC_KEY.to_bytes(4, "little")


def test_crc_matches_zlib():
    import zlib
    payload = b"\x01\x02\x03"
    key = 0x35769521
    assert x1.crc32(key, payload) == zlib.crc32(payload, key) & 0xFFFFFFFF


def test_pack_bits_polarity():
    img = Image.new("L", (8, 1), 255)
    img.putpixel((0, 0), 0)
    img.putpixel((7, 0), 0)
    assert x1._pack_bits(img) == bytes([0b10000001])


def test_shift_left_pads_right_with_white():
    img = Image.new("L", (16, 1), 255)
    img.putpixel((15, 0), 0)          # 最右一个黑点
    shifted = x1.shift_x(img, -8)    # 左移 8 点
    assert shifted.getpixel((7, 0)) == 0
    assert shifted.getpixel((15, 0)) == 255


def test_otsu_threshold_separates_bimodal_image():
    img = Image.new("L", (64, 64), 250)
    ImageDraw.Draw(img).rectangle([8, 8, 40, 40], fill=10)
    t = x1.otsu_threshold(img)
    assert 10 <= t < 250          # 阈值落在两个灰度之间

    binarized = x1.prepare(img, "text")   # 会被缩放成 384 宽
    assert binarized.getpixel((200, 200)) == 0      # 深色块中心 → 黑
    assert binarized.getpixel((10, 10)) == 255      # 边角背景 → 白


def test_text_min_font_clamped():
    assert x1.MIN_FONT_SIZE == 14
    img = x1.render_text("测试", 6)
    assert img.width == x1.DOT_WIDTH


def test_layout_renders_something(tmp_path):
    from layout import render_layout

    sample = tmp_path / "page.yaml"
    sample.write_text(
        "theme: minimal\nblocks:\n"
        "  - type: title\n    text: 标题\n"
        "  - type: divider\n    style: dots\n"
        "  - type: kv\n    items:\n      - [键, 值]\n",
        encoding="utf-8",
    )
    img = render_layout(sample)
    assert img.width == x1.DOT_WIDTH
    assert img.height > 40


def test_qr_and_barcode_blocks():
    qr = x1.make_qr("https://example.com", "扫码")
    assert qr.width == x1.DOT_WIDTH and qr.height > 200
    bc = x1.make_barcode("X1-20261122", caption="X1-20261122")
    assert bc.width == x1.DOT_WIDTH and bc.height > 60

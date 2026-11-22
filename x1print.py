#!/usr/bin/env python
"""相印宝 X1 命令行打印工具（Windows，蓝牙 BLE 直连）。

用法示例：
    python x1print.py --scan
    python x1print.py --status
    python x1print.py "你好，相印宝"
    python x1print.py --font 26 便签.txt
    python x1print.py --image 照片.jpg --dither
    python x1print.py --pdf 文档.pdf
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PAPER_DENSITY = {"plain": 95, "sticker": 75}  # 实测：不干胶 75 ≈ 普通纸 95
from layout import render_layout
from x1 import (binarized_preview, compose_vertical, make_barcode, make_qr, render_text, DOTS_PER_MM, DEFAULT_FEED_LINES, DENSITY_PRESET, MM_PER_FEED_LINE,
                STATUS_BITS, X1Printer, X1Serial, discover)

CONF = Path(__file__).with_name(".x1_mac")


def make_printer(mac: str | None, args: argparse.Namespace):
    """优先经典蓝牙 SPP（和官方 App 同一条通道，出纸连续）；没有配对串口时退回 BLE。"""
    if getattr(args, "ble", False):
        return X1Printer(mac)
    port = args.port or X1Serial.find_port(mac)
    if port:
        try:
            printer = X1Serial(port)
            print(f"用经典蓝牙串口 {port}（和官方 App 一样，速度最快）", file=sys.stderr)
            return printer
        except Exception as e:
            print(f"串口 {port} 打不开（{e}）：打印机没开机、或被手机 App 占着？先退回 BLE",
                  file=sys.stderr)
    print("没找到蓝牙串口，改用 BLE（慢一些，可能一段一段走纸）", file=sys.stderr)
    return X1Printer(mac)


def load_mac() -> str | None:
    return CONF.read_text(encoding="utf-8").strip() if CONF.exists() else None


def save_mac(mac: str) -> None:
    CONF.write_text(mac, encoding="utf-8")


async def resolve_addr(mac: str | None) -> str:
    if mac:
        return mac
    saved = load_mac()
    if saved:
        return saved
    print("未指定 MAC，正在扫描附近的相印宝…", file=sys.stderr)
    addr = await discover()
    if not addr:
        sys.exit("没找到相印宝 X1：请确认打印机已开机，且没有被手机 App 占着连接。")
    save_mac(addr)
    print(f"已记住打印机：{addr}", file=sys.stderr)
    return addr


def describe_status(byte: int) -> str:
    if byte == 0:
        return "正常 (0x00)"
    bits = [name for bit, name in STATUS_BITS.items() if byte & bit]
    return f"0x{byte:02x} " + ("、".join(bits) if bits else "未知")


IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".ini", ".json", ".yaml", ".yml",
            ".xml", ".html", ".py", ".c", ".h", ".java", ".js", ".ts", ".go", ".rs"}


def normalize_args(args: argparse.Namespace) -> None:
    """把「位置参数是一个文件」的情况分派到正确的打印路径，避免把图片当文本读。"""
    if not args.content:
        return
    p = Path(args.content)
    if not p.is_file():
        return
    suffix = p.suffix.lower()
    if suffix in IMG_EXT:
        args.image = args.image or str(p)
        args.content = None
    elif suffix in TEXT_EXT:
        args.content = p.read_text(encoding="utf-8", errors="replace")
    else:
        sys.exit(f"不认识的文件类型「{suffix or '无扩展名'}」：图片用 --image，PDF 用 --pdf，"
                 f"文本请改扩展名为 .txt/.md")


async def main_async(args: argparse.Namespace) -> int:
    normalize_args(args)
    if args.scan:
        addr = await discover()
        print(f"找到：{addr}" if addr else "没扫描到相印宝 X1")
        if addr and not args.no_save:
            save_mac(addr)
        return 0

    # 只预览（--no-print）时不需要设备地址，避免在无蓝牙环境里扫描（CI 也能跑）
    addr = args.mac if args.no_print else await resolve_addr(args.mac)
    printer = None
    if not args.no_print:
        printer = make_printer(addr, args)
        await printer.connect()
    try:
        if args.status and printer is not None:
            print("状态:", describe_status(await printer.status() or 0))
            print("信息:", await printer.info())
            return 0

        level = None
        if args.paper and args.density is None:
            level = PAPER_DENSITY[args.paper]
        if args.density is not None:
            level = DENSITY_PRESET.get(args.density)
            if level is None:
                try:
                    level = int(args.density)
                except ValueError:
                    sys.exit("--density 只能是 light/middle/dark 或 0~100 的整数")
        if level is not None:
            if not 0 <= level <= 100:
                sys.exit("浓度范围 0~100")
            if printer is not None:
                await printer.set_density(level)
                print(f"浓度设为 {level}")

        feed_lines = args.feed
        if args.gap_mm is not None:
            feed_lines = max(0, round(args.gap_mm / MM_PER_FEED_LINE))
        if feed_lines is None:
            feed_lines = DEFAULT_FEED_LINES
        lead_lines = max(0, round(args.lead_mm / MM_PER_FEED_LINE)) if args.lead_mm else 0
        shift_dots = round(args.shift_mm * DOTS_PER_MM)
        photo_mode = args.dither or args.mode == "photo"
        smooth = args.smooth if args.smooth is not None else photo_mode

        if printer is not None and not args.force:
            st = await printer.status()
            if st:
                print(f"打印机状态异常：{describe_status(st)}——已中止（--force 可强制打印）",
                      file=sys.stderr)
                return 3

        if not (args.content or args.image or args.pdf or args.qr or args.barcode or args.layout):
            if feed_lines and printer is not None:
                await printer.feed(feed_lines)
                print(f"走纸 {feed_lines} 行 ≈ {feed_lines * MM_PER_FEED_LINE} mm")
                return 0
            print("没有可打印的内容（给一段文字、--image、--qr 或 --pdf）", file=sys.stderr)
            return 2

        def maybe_preview(img, name: str) -> None:
            if not args.preview:
                return
            stem = Path(name).stem or "print"
            out = Path(f"{stem}.preview.png")
            binarized_preview(img, args.mode, args.sharpen, shift_dots, smooth).save(out)
            print(f"预览（即将发送的位图）: {out}")

        if args.layout:
            page = render_layout(args.layout)
            maybe_preview(page, args.layout)
            if not args.no_print:
                await printer.print_image(page, feed_lines=feed_lines,
                                          mode=args.mode, lead_lines=lead_lines,
                                          sharpen=args.sharpen, shift_dots=shift_dots,
                                          smooth=smooth)
                print(f"布局已发送：{args.layout}")
            return 0

        if args.content or args.qr or args.barcode:
            blocks = []
            if args.content:
                blocks.append(render_text(args.content, args.font))
            if args.qr:
                blocks.append(make_qr(args.qr, args.qr_caption))
            if args.barcode:
                blocks.append(make_barcode(args.barcode, args.barcode_fmt, args.barcode_caption))
            page = compose_vertical(blocks) if len(blocks) > 1 else blocks[0]
            maybe_preview(page, "text")
            if not args.no_print:
                await printer.print_image(page, feed_lines=feed_lines,
                                          mode=args.mode, lead_lines=lead_lines,
                                          sharpen=args.sharpen, shift_dots=shift_dots,
                                          smooth=smooth)
                print("内容已发送")

        if args.image:
            from PIL import Image
            img = Image.open(args.image)
            maybe_preview(img, args.image)
            if not args.no_print:
                await printer.print_image(img, feed_lines=feed_lines,
                                          mode="photo" if args.dither else args.mode,
                                          lead_lines=lead_lines, sharpen=args.sharpen,
                                          shift_dots=shift_dots, smooth=smooth)
                print(f"图片已发送：{args.image}")

        if args.pdf:
            try:
                import fitz  # PyMuPDF
            except ImportError:
                sys.exit("打印 PDF 需要 PyMuPDF：python -m pip install pymupdf")
            from PIL import Image
            doc = fitz.open(args.pdf)
            for i, page in enumerate(doc, 1):
                pix = page.get_pixmap(dpi=203)
                tmp = Path(args.pdf).with_suffix(f".p{i}.png")
                pix.save(tmp)
                img = Image.open(tmp)
                maybe_preview(img, f"{Path(args.pdf).stem}_p{i}")
                if not args.no_print:
                    await printer.print_image(img, feed_lines=feed_lines,
                                              mode="photo" if args.dither else args.mode,
                                              lead_lines=lead_lines, sharpen=args.sharpen,
                                              shift_dots=shift_dots, smooth=smooth)
                    print(f"第 {i} 页已发送")
            return 0
        return 0
    finally:
        if printer is not None:
            await printer.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x1print", description="相印宝 X1 打印机（经典蓝牙串口 / BLE 直连）")
    ap.add_argument("content", nargs="?", help="要打印的文字或 .txt/.md 文件路径")
    ap.add_argument("--layout", help="用 YAML 布局打印整页（支持 title/text/divider/kv/qr/barcode/image/space）")
    ap.add_argument("--qr", help="在文字下方加一个二维码（内容是这段文字）")
    ap.add_argument("--qr-caption", help="二维码下面的说明文字")
    ap.add_argument("--barcode", help="打印 CODE128 条码，例如 --barcode X1-2026")
    ap.add_argument("--barcode-fmt", default="code128", help="条码格式，默认 code128")
    ap.add_argument("--barcode-caption", help="条码下面的说明文字")
    ap.add_argument("--image", "-i", help="打印图片")
    ap.add_argument("--pdf", help="打印 PDF（逐页）")
    ap.add_argument("--scan", action="store_true", help="扫描并记住打印机")
    ap.add_argument("--status", action="store_true", help="查询状态与设备信息")
    ap.add_argument("--mac", help="指定打印机 MAC（默认用上次的）")
    ap.add_argument("--port", help="指定蓝牙串口，如 COM7（配对后自动识别，一般不用填）")
    ap.add_argument("--ble", action="store_true", help="强制走 BLE，不用蓝牙串口")
    ap.add_argument("--font", type=int, default=30, help="文字字号，默认 30（最小 14，更小已看不清）")
    ap.add_argument("--paper", choices=("plain", "sticker"), help="按纸张给默认浓度：plain=95，sticker=75（--density 优先）")
    ap.add_argument("--density", help="浓度：light/middle/dark（55/75/95）或 0~100")
    ap.add_argument("--feed", type=int, help="打印后走纸行数（1 行=3mm；默认 10 行=30mm，与官方一致；要更长可用 --gap-mm）")
    ap.add_argument("--gap-mm", type=float, help="改用毫米指定打印后空白，例如 --gap-mm 30")
    ap.add_argument("--lead-mm", type=float, help="打印前先走纸多少毫米，例如 --lead-mm 5")
    ap.add_argument("--mode", choices=("text", "photo", "bayer", "hard"), default="text",
                    help="text=黑白阈值（文档/文字）；photo=抖动（照片）")
    ap.add_argument("--shift-mm", type=float, default=-0.5, help="整幅水平平移毫米数，正=右 负=左（默认 -0.5，因你的纸窄 1mm）")
    ap.add_argument("--smooth", dest="smooth", action="store_true", default=None,
                    help="自动对比度 + 轻微模糊（photo 模式默认已开启）")
    ap.add_argument("--no-smooth", dest="smooth", action="store_false",
                    help="photo 模式关闭平滑")
    ap.add_argument("--sharpen", action="store_true", help="二值化前轻锐化（203dpi 下照片/细线更清楚）")
    ap.add_argument("--preview", action="store_true", help="把即将发送的黑白位图另存为 *.preview.png")
    ap.add_argument("--no-print", action="store_true", help="只生成预览，不真的打印")
    ap.add_argument("--force", action="store_true", help="忽略状态异常（缺纸/开盖）强制打印")
    ap.add_argument("--dither", action="store_true", help="等价于 --mode photo")
    ap.add_argument("--version", action="version", version="x1print 0.1.1")
    ap.add_argument("--no-save", action="store_true", help="扫描后不记住 MAC")
    return ap


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(build_parser().parse_args())))

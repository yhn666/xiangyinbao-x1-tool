"""得力相印宝 X1 / HPRT BLE 打印机驱动（Windows，bleak）。

协议来自官方 APK（com.feioou.print，cpcl 包）：
    帧 = 0x02 | cmd | pkg | len(LE16) | payload | crc32(LE32) | 0x03
    crc32 的初始值 = 当前密钥（默认 0x35769521，握手后换成随机密钥）
"""
from __future__ import annotations

import asyncio
import random
import struct
import zlib
from pathlib import Path
from typing import Iterable, Sequence

from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont

ADDR_DEFAULT = "00:15:83:C2:87:9A"
NAME_PREFIXES = ("XiangYinBao", "X1", "XYB")
CH_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"
CH_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"

STX, ETX = 0x02, 0x03
CMD_PRINT = 0x00          # 位图行数据（每包 30 行）
CMD_STATUS = 0x0C         # 状态
CMD_DENSITY = 0x19        # 26? -> setPrintDensity 用的是 25
CMD_FEED = 0x1A           # 走纸（值 = 行数*24 点）
CMD_CRC_KEY = 0x18        # 交换 CRC 密钥
CMD_MODEL = 0x06
CMD_SN = 0x0A
CMD_VERSION = 0x04
DEFAULT_CRC_KEY = 0x35769521

DOT_WIDTH = 384           # 48mm / 203dpi
ROWS_PER_PACKET = 30
STATUS_BITS = {0x01: "缺纸", 0x02: "打印头过热", 0x04: "电量低", 0x08: "开盖",
               0x10: "电池异常"}

# 官方 App「深浅浓度」三档（PrintUtil.setNormalPrintDensti -> Print.setPrintDensity）
DENSITY_PRESET = {"light": 55, "middle": 75, "dark": 95}
# 官方 App 打印结束后固定调用 Print.setPrintFeedLine(10)：10 * 8 * 3 = 240 点 ≈ 30 mm
DEFAULT_FEED_LINES = 10   # 官方值：30mm 走纸（打印头到撕口约 33mm，纸面约留 3mm）
MM_PER_FEED_LINE = 3  # 一条“行”= 24 点 = 3 mm
DOTS_PER_MM = 8       # 203dpi
DEFAULT_SHIFT_MM = -0.5   # 用户纸比官方窄 1mm：先左移 1mm，实测左 5mm/右 6mm，再右移 0.5mm 取中
DEFAULT_SHIFT_DOTS = round(DEFAULT_SHIFT_MM * DOTS_PER_MM)

# BLE 写入节奏（Windows WinRT 实测）：
#   每个 write 都 sleep → 卡在蓝牙连接间隔上，只有 ~1.3KB/s，出纸一顿一顿
#   完全不等 → 35KB/s，但模块串口缓冲会溢出，整帧被丢掉
#   折中：每 WRITE_BURST 字节猛发一次，再停 WRITE_PAUSE 等缓冲排空（≈9KB/s）
WRITE_CHUNK = 20
WRITE_BURST = 240
WRITE_PAUSE = 0.02
WRITE_RESPONSE = False


def crc32(key: int, data: bytes) -> int:
    return zlib.crc32(data, key) & 0xFFFFFFFF


def build_frame(cmd: int, pkg: int, payload: bytes, key: int) -> bytes:
    body = bytes([STX, cmd & 0xFF, pkg & 0x7F]) + struct.pack("<H", len(payload)) + payload
    return body + struct.pack("<I", crc32(key, payload)) + bytes([ETX])


async def discover(timeout: float = 8.0) -> str | None:
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        name = d.name or ""
        if any(name.startswith(p) for p in NAME_PREFIXES) or "XiangYinBao" in name:
            return d.address
    return None


def _pack_bits(img: Image.Image) -> bytes:
    """1bpp 顶行在前，黑=1（打印机把置位的点打成黑）。"""
    w, h = img.size
    px = img.load()
    row_bytes = (w + 7) // 8
    out = bytearray(row_bytes * h)
    for y in range(h):
        base = y * row_bytes
        for x in range(w):
            if px[x, y] < 128:          # 亮度低 = 黑点
                out[base + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out)


MIN_FONT_SIZE = 14  # 实测 12 号以下中文已糊成一团

FONT_SANS_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",          # 微软雅黑
    "C:/Windows/Fonts/msyh.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
FONT_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhbd.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
FONT_SERIF_CANDIDATES = [
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/Songti.ttc",
]


def load_font(size: int, kind: str = "sans", prefer: str | None = None) -> ImageFont.ImageFont:
    """按平台挑一个能用的中文字体；都没有就退回 Pillow 内置位图字体。"""
    pool = {"sans": FONT_SANS_CANDIDATES,
            "bold": FONT_BOLD_CANDIDATES,
            "serif": FONT_SERIF_CANDIDATES}.get(kind, FONT_SANS_CANDIDATES)
    for candidate in ([prefer] if prefer else []) + pool:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()



def render_text(text: str, font_size: int = 30, font_path: str | None = None,
                margin: int = 16) -> Image.Image:
    font_size = max(MIN_FONT_SIZE, int(font_size))
    font = load_font(font_size, prefer=font_path)
    max_w = DOT_WIDTH - margin * 2

    lines: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw:
            lines.append("")
            continue
        cur = ""
        for ch in raw:
            if font.getlength(cur + ch) <= max_w:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)

    line_h = int(font_size * 1.35)
    img = Image.new("L", (DOT_WIDTH, max(line_h, line_h * len(lines) + margin * 2)), 255)
    draw = ImageDraw.Draw(img)
    y = margin
    for line in lines:
        draw.text((margin, y), line, font=font, fill=0)
        y += line_h
    return img


BAYER8 = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37),
    (63, 31, 55, 23, 61, 29, 53, 21),
)


def otsu_threshold(img: Image.Image) -> int:
    hist = img.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best_t, best_var = 128, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def shift_x(img: Image.Image, dots: int) -> Image.Image:
    """整幅左右平移 dots 点（正=右，负=左），空出来的边填白。"""
    if dots == 0:
        return img
    w, h = img.size
    out = Image.new("L", (w, h), 255)
    if dots < 0:
        n = min(-dots, w)
        out.paste(img.crop((n, 0, w, h)), (0, 0))
    else:
        n = min(dots, w)
        out.paste(img.crop((0, 0, w - n, h)), (n, 0))
    return out


def prepare(img: Image.Image, mode: str = "text", sharpen: bool = False,
            shift_dots: int = 0, smooth: bool = False) -> Image.Image:
    """按官方 App 的几种二值化方式来准备 384 点宽的位图。

    text  = Otsu 阈值（官方「纯文字 / 增强文字」走的就是阈值法）
    photo = Floyd–Steinberg 抖动（官方「图文 / 图片」模式）
    bayer = 8x8 有序抖动（官方 PrinterDataCore 的第三种模式）
    hard  = 固定 128 硬阈值
    shift_dots: 整幅水平平移（1mm = 8 点，负数向左）
    smooth: 先自动对比度 + 轻微高斯模糊，抖动颗粒更细（照片常用）
    """
    img = img.convert("L")
    w, h = img.size
    if w != DOT_WIDTH:
        nh = max(1, int(h * DOT_WIDTH / w))
        img = img.resize((DOT_WIDTH, nh), Image.Resampling.LANCZOS)
    if smooth:
        from PIL import ImageFilter, ImageOps
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
    if sharpen:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=150, threshold=2))

    if mode == "photo":
        out = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    elif mode == "bayer":
        t = otsu_threshold(img)
        px = img.load()
        out = img.copy()
        opx = out.load()
        for y in range(img.height):
            row = BAYER8[y & 7]
            for x in range(img.width):
                v = px[x, y] + (row[x & 7] - 32) * 2
                opx[x, y] = 0 if v <= t else 255
    else:
        t = 128 if mode == "hard" else otsu_threshold(img)
        out = img.point(lambda x: 0 if x <= t else 255)
    return shift_x(out, shift_dots)


def binarized_preview(img: Image.Image, mode: str = "text",
                      sharpen: bool = False, shift_dots: int = 0, smooth: bool = False) -> Image.Image:
    """生成「即将发给打印机的 384 点黑白位图」预览，像素与实打一致。"""
    return prepare(img, mode, sharpen, shift_dots, smooth).convert("1")


def compose_vertical(blocks: list[Image.Image], gap: int = 14) -> Image.Image:
    """把多块内容竖着拼成一张 384 宽的页。"""
    h = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
    canvas = Image.new("L", (DOT_WIDTH, h), 255)
    y = 0
    for b in blocks:
        canvas.paste(b, (0, y))
        y += b.height + gap
    return canvas


def make_qr(data: str, caption: str | None = None, size: int = 224) -> Image.Image:
    """生成居中的二维码块（含可选说明文字）。"""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("L")
    img = img.resize((size, size), Image.Resampling.NEAREST)

    h = size + 10 + (34 if caption else 0)
    canvas = Image.new("L", (DOT_WIDTH, h), 255)
    canvas.paste(img, ((DOT_WIDTH - size) // 2, 4))
    if caption:
        font = load_font(18)
        d = ImageDraw.Draw(canvas)
        w = d.textlength(caption, font=font)
        d.text(((DOT_WIDTH - w) // 2, size + 8), caption, font=font, fill=0)
    return canvas


def make_barcode(data: str, fmt: str = "code128", caption: str | None = None) -> Image.Image:
    """生成条码块（默认 CODE128，宽度铺满 384 点）。"""
    from barcode import get_barcode_class
    from barcode.writer import ImageWriter

    cls = get_barcode_class(fmt)
    bc = cls(data, writer=ImageWriter())
    img = bc.render(writer_options={"module_width": 0.2, "module_height": 12,
                                    "quiet_zone": 2.0, "write_text": False}).convert("L")
    img = img.resize((DOT_WIDTH, max(1, int(img.height * DOT_WIDTH / img.width))),
                     Image.Resampling.LANCZOS)
    h = img.height + 10 + (34 if caption else 0)
    canvas = Image.new("L", (DOT_WIDTH, h), 255)
    canvas.paste(img, (0, 4))
    if caption:
        font = load_font(18)
        d = ImageDraw.Draw(canvas)
        w = d.textlength(caption, font=font)
        d.text(((DOT_WIDTH - w) // 2, img.height + 8), caption, font=font, fill=0)
    return canvas


class _ProtocolTransport:
    """两条传输共享的协议层：帧收发、握手、状态、浓度、走纸、打印。

    子类只需要实现 connect/close/_write/_fill。
    """

    def __init__(self) -> None:
        self.rx = bytearray()
        self.key = DEFAULT_CRC_KEY
        self.pkg = 1

    # --- 子类实现 ---
    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def _write(self, data: bytes) -> None:
        raise NotImplementedError

    def _fill(self) -> None:
        """把底层收到的数据搬进 self.rx（SPP 用；BLE 由回调直接写入）。"""

    async def __aenter__(self) -> "_ProtocolTransport":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # --- 共用逻辑 ---
    def _next_pkg(self) -> int:
        val = self.pkg
        self.pkg = 1 if self.pkg >= 127 else self.pkg + 1
        return val

    def _pop_frame(self) -> bytes | None:
        self._fill()
        while True:
            start = self.rx.find(bytes([STX]))
            if start < 0:
                self.rx.clear()
                return None
            if len(self.rx) < start + 6:
                return None
            length = struct.unpack_from("<H", self.rx, start + 3)[0]
            end = start + 10 + length          # STX+cmd+pkg+len(2)+payload+crc(4)+ETX
            if len(self.rx) < end:
                return None
            frame = bytes(self.rx[start:end])
            del self.rx[:end]
            if frame[-1] == ETX:
                return frame

    async def request(self, cmd: int, payload: bytes = b"\x00",
                      timeout: float = 2.0) -> bytes | None:
        await self._write(build_frame(cmd, self._next_pkg(), payload, self.key))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            frame = self._pop_frame()
            if frame and frame[1] == cmd:
                length = struct.unpack_from("<H", frame, 3)[0]
                return frame[5:5 + length]
            await asyncio.sleep(0.02)
        return None

    async def handshake(self, timeout: float = 2.0) -> bool:
        """用随机密钥换掉默认 CRC 初值；设备回 0 表示成功。"""
        self.key = DEFAULT_CRC_KEY
        new_key = random.randint(2, 0x7FFFFFFF)
        payload = struct.pack("<I", new_key ^ DEFAULT_CRC_KEY)
        resp = await self.request(CMD_CRC_KEY, payload, timeout=timeout)
        if resp and resp[0] == 0:
            self.key = new_key
            return True
        return False

    async def status(self) -> int | None:
        resp = await self.request(CMD_STATUS, b"\x00")
        return resp[0] if resp else None

    async def info(self) -> dict:
        v = await self.request(CMD_VERSION, b"\x00")
        s = await self.request(CMD_SN, b"\x00")
        m = await self.request(CMD_MODEL, b"\x00")
        return {
            "version": ".".join(str(b) for b in v[:3]) if v else None,
            "sn": s.decode("ascii", "ignore") if s else None,
            "model": m.decode("ascii", "ignore") if m else None,
        }

    async def set_density(self, level: int) -> None:
        """浓度 0~100（官方三档 55/75/95）。和 App 一样不等 ACK。"""
        await self._write(build_frame(CMD_DENSITY, self._next_pkg(), bytes([level & 0xFF]), self.key))
        await asyncio.sleep(0.1)

    async def feed(self, lines: int = DEFAULT_FEED_LINES) -> None:
        """走纸 lines 条“行”，每条 24 点 = 3mm。"""
        payload = struct.pack("<H", (lines * 8) * 3)
        await self._write(build_frame(CMD_FEED, self._next_pkg(), payload, self.key))

    async def print_image(self, img: Image.Image, feed_lines: int = DEFAULT_FEED_LINES,
                          mode: str = "text", lead_lines: int = 0, sharpen: bool = False,
                          shift_dots: int = DEFAULT_SHIFT_DOTS, smooth: bool = False) -> None:
        if lead_lines:
            await self.feed(lead_lines)
            await asyncio.sleep(0.2)
        bits = _pack_bits(prepare(img, mode, sharpen, shift_dots, smooth))
        step = (DOT_WIDTH // 8) * ROWS_PER_PACKET
        for i in range(0, len(bits), step):
            await self._write(build_frame(CMD_PRINT, self._next_pkg(), bits[i:i + step], self.key))
        if feed_lines:
            await asyncio.sleep(0.15)
            await self.feed(feed_lines)

    async def print_text(self, text: str, font_size: int = 30,
                         feed_lines: int = DEFAULT_FEED_LINES, lead_lines: int = 0) -> None:
        await self.print_image(render_text(text, font_size), feed_lines=feed_lines,
                               lead_lines=lead_lines)


class X1Serial(_ProtocolTransport):
    """经典蓝牙 SPP（官方 App 走的通道）：Windows 配对后是虚拟串口，整帧灌入、出纸连续。"""

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        super().__init__()
        import serial  # pyserial

        self.port = port
        self.ser = serial.Serial(port, baudrate, timeout=0.2, write_timeout=5)

    @staticmethod
    def find_port(mac: str | None = None) -> str | None:
        """按 MAC 找配对后生成的蓝牙串口，避免误抓到别的蓝牙串口。"""
        from serial.tools import list_ports

        want = mac.replace(":", "").upper() if mac else None
        for p in list_ports.comports():
            hwid = (p.hwid or "").upper()
            if "BTHENUM" not in hwid:
                continue
            if want and want in hwid:
                return p.device
            if not want:
                return p.device
        return None

    async def connect(self) -> None:
        if not await self.handshake():
            raise RuntimeError("CRC 密钥握手失败")

    async def close(self) -> None:
        self.ser.close()

    async def _write(self, data: bytes) -> None:
        self.ser.write(data)

    def _fill(self) -> None:
        data = self.ser.read(max(1, self.ser.in_waiting))
        if data:
            self.rx.extend(data)


class X1Printer(_ProtocolTransport):
    """BLE 直连（没有配对串口时的兜底）。"""

    def __init__(self, addr: str | None = None) -> None:
        super().__init__()
        self.addr = addr or ADDR_DEFAULT
        self.client: BleakClient | None = None

    async def connect(self) -> None:
        self.client = BleakClient(self.addr, timeout=25.0)
        await self.client.connect()
        await self.client.start_notify(CH_NOTIFY, self._on_notify)
        await asyncio.sleep(0.3)
        self.rx.clear()
        if not await self.handshake():
            raise RuntimeError("CRC 密钥握手失败")

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()
            self.client = None

    def _on_notify(self, _sender, data: bytearray) -> None:
        self.rx.extend(data)

    async def _write(self, data: bytes) -> None:
        """BLE 上「猛发一小段 + 停一下」：WinRT 逐包等应答只有 ~1KB/s，无应答猛发会丢包。"""
        assert self.client is not None
        for i in range(0, len(data), WRITE_BURST):
            seg = data[i:i + WRITE_BURST]
            for j in range(0, len(seg), WRITE_CHUNK):
                await self.client.write_gatt_char(CH_WRITE, seg[j:j + WRITE_CHUNK],
                                                 response=WRITE_RESPONSE)
            if WRITE_PAUSE:
                await asyncio.sleep(WRITE_PAUSE)

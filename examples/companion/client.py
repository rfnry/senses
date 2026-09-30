import asyncio
import math
import struct

from rfnry_senses import AudioFrame, Loopback

from providers import GoogleSpeech

voice = GoogleSpeech(voice="Puck")


async def ana_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    for _ in range(20):
        await link.speak(AudioFrame(data=bytes(4800), sample_rate=24000))
        await asyncio.sleep(0.1)


async def film(link: Loopback, frame: bytes) -> None:
    while True:
        await link.show(frame)
        await asyncio.sleep(1)


ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
]  # fmt: skip
QUANT = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
]  # fmt: skip
AC = [0x00, 0xF0] + [run << 4 | size for run in range(16) for size in range(1, 11)]
COSINES = [
    [
        (math.sqrt(0.5) if u == 0 else 1) / 2 * math.cos((2 * x + 1) * u * math.pi / 16)
        for x in range(8)
    ]
    for u in range(8)
]


def scene() -> list[list[tuple[int, int, int]]]:
    width, height = 320, 240
    wall, wood = (225, 218, 205), (150, 105, 60)
    red, blue, navy, yellow, green = (
        (200, 30, 35),
        (35, 70, 170),
        (20, 40, 110),
        (245, 215, 50),
        (90, 170, 50),
    )
    pixels = [[wall if y < 96 else wood for _ in range(width)] for y in range(height)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            pixels[y][x0:x1] = [color] * (x1 - x0)

    def disc(cx: int, cy: int, r: int, color: tuple[int, int, int], hole: int = 0) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if hole * hole <= (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    pixels[y][x] = color

    disc(100, 150, 22, red, hole=12)
    rect(40, 110, 100, 190, red)
    rect(140, 130, 250, 210, blue)
    rect(140, 130, 152, 210, navy)
    rect(262, 108, 302, 148, yellow)
    disc(284, 192, 22, green)
    return pixels


class Bits:
    def __init__(self) -> None:
        self.out, self.word, self.count = bytearray(), 0, 0

    def put(self, value: int, length: int) -> None:
        self.word, self.count = self.word << length | value, self.count + length
        while self.count >= 8:
            self.count -= 8
            byte = self.word >> self.count & 0xFF
            self.out += b"\xff\x00" if byte == 0xFF else bytes([byte])

    def flush(self) -> bytes:
        if self.count:
            self.put((1 << (8 - self.count)) - 1, 8 - self.count)
        return bytes(self.out)


def category(value: int) -> tuple[int, int]:
    size = abs(value).bit_length()
    return size, value if value >= 0 else value + (1 << size) - 1


def block(bits: Bits, samples: list[list[float]], previous: int) -> int:
    rows = [
        [sum(COSINES[v][y] * samples[y][x] for y in range(8)) for x in range(8)] for v in range(8)
    ]
    table = [sum(rows[v][x] * COSINES[u][x] for x in range(8)) for v in range(8) for u in range(8)]
    coefficients = [round(table[i] / QUANT[i]) for i in ZIGZAG]
    size, bits_value = category(coefficients[0] - previous)
    bits.put(size, 4)
    bits.put(bits_value, size)
    run = 0
    for value in coefficients[1:]:
        if value == 0:
            run += 1
            continue
        while run > 15:
            bits.put(AC.index(0xF0), 8)
            run -= 16
        size, bits_value = category(value)
        bits.put(AC.index(run << 4 | size), 8)
        bits.put(bits_value, size)
        run = 0
    if run:
        bits.put(AC.index(0x00), 8)
    return coefficients[0]


def segment(marker: int, body: bytes) -> bytes:
    return struct.pack(">HH", marker, len(body) + 2) + body


def desk() -> bytes:
    return encode(scene())


def encode(pixels: list[list[tuple[int, int, int]]]) -> bytes:
    height, width = len(pixels), len(pixels[0])
    planes = [
        [[0.299 * r + 0.587 * g + 0.114 * b - 128 for r, g, b in row] for row in pixels],
        [[-0.168736 * r - 0.331264 * g + 0.5 * b for r, g, b in row] for row in pixels],
        [[0.5 * r - 0.418688 * g - 0.081312 * b for r, g, b in row] for row in pixels],
    ]
    bits, previous = Bits(), [0, 0, 0]
    for top in range(0, height, 8):
        for left in range(0, width, 8):
            for c, plane in enumerate(planes):
                samples = [row[left : left + 8] for row in plane[top : top + 8]]
                previous[c] = block(bits, samples, previous[c])
    frame = struct.pack(">BHHB", 8, height, width, 3) + bytes([1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0])
    huffman = (
        bytes([0x00]) + bytes([0, 0, 0, 12] + [0] * 12) + bytes(range(12))
        + bytes([0x10]) + bytes([0] * 7 + [len(AC)] + [0] * 8) + bytes(AC)
    )  # fmt: skip
    return (
        b"\xff\xd8"
        + segment(0xFFE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        + segment(0xFFDB, bytes([0]) + bytes(QUANT[i] for i in ZIGZAG))
        + segment(0xFFC0, frame)
        + segment(0xFFC4, huffman)
        + segment(0xFFDA, bytes([3, 1, 0x00, 2, 0x00, 3, 0x00, 0, 63, 0]))
        + bits.flush()
        + b"\xff\xd9"
    )


GLYPHS = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    " ": ["00000"] * 7,
}


def screen() -> bytes:
    width, height = 480, 320
    desktop, title, bar, tools, canvas = (
        (70, 90, 110),
        (45, 45, 50),
        (232, 232, 235),
        (60, 60, 66),
        (250, 250, 250),
    )
    ink, white, grey, blue = (30, 30, 35), (255, 255, 255), (200, 200, 205), (30, 110, 230)
    red, amber, green, orange, teal = (
        (235, 90, 80),
        (240, 190, 50),
        (90, 190, 80),
        (245, 140, 40),
        (40, 160, 150),
    )
    pixels = [[desktop] * width for _ in range(height)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            pixels[y][x0:x1] = [color] * (x1 - x0)

    def disc(cx: int, cy: int, r: int, color: tuple[int, int, int]) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    pixels[y][x] = color

    def write(x: int, y: int, text: str, color: tuple[int, int, int]) -> None:
        for n, letter in enumerate(text):
            for row, line in enumerate(GLYPHS[letter]):
                for col, bit in enumerate(line):
                    if bit == "1":
                        rect(
                            x + n * 12 + col * 2,
                            y + row * 2,
                            x + n * 12 + col * 2 + 2,
                            y + row * 2 + 2,
                            color,
                        )

    rect(8, 8, width - 8, height - 8, bar)
    rect(8, 8, width - 8, 32, title)
    for n, color in enumerate((red, amber, green)):
        disc(22 + n * 16, 20, 5, color)
    write(16, 44, "FILE  EDIT  VIEW", ink)
    rect(318, 38, 384, 64, grey)
    write(328, 44, "OPEN", ink)
    rect(394, 38, 464, 64, blue)
    write(405, 44, "SAVE", white)
    rect(8, 72, 48, height - 8, tools)
    for n in range(6):
        rect(18, 84 + n * 32, 38, 104 + n * 32, grey)
    rect(56, 72, width - 16, height - 16, canvas)
    disc(170, 190, 60, orange)
    rect(260, 130, 400, 270, teal)
    return encode(pixels)

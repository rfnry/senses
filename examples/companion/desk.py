import math
import struct

WIDTH, HEIGHT = 320, 240
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

WALL, WOOD = (225, 218, 205), (150, 105, 60)
RED, BLUE, NAVY, YELLOW, GREEN = (
    (200, 30, 35),
    (35, 70, 170),
    (20, 40, 110),
    (245, 215, 50),
    (90, 170, 50),
)


def scene() -> list[list[tuple[int, int, int]]]:
    pixels = [[WALL if y < 96 else WOOD for _ in range(WIDTH)] for y in range(HEIGHT)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            pixels[y][x0:x1] = [color] * (x1 - x0)

    def disc(cx: int, cy: int, r: int, color: tuple[int, int, int], hole: int = 0) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if hole * hole <= (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    pixels[y][x] = color

    disc(100, 150, 22, RED, hole=12)
    rect(40, 110, 100, 190, RED)
    rect(140, 130, 250, 210, BLUE)
    rect(140, 130, 152, 210, NAVY)
    rect(262, 108, 302, 148, YELLOW)
    disc(284, 192, 22, GREEN)
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

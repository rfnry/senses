from desk import encode

WIDTH, HEIGHT = 480, 320
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
DESKTOP, TITLE, BAR, TOOLS, CANVAS = (
    (70, 90, 110),
    (45, 45, 50),
    (232, 232, 235),
    (60, 60, 66),
    (250, 250, 250),
)
INK, WHITE, GREY, BLUE = (30, 30, 35), (255, 255, 255), (200, 200, 205), (30, 110, 230)
RED, AMBER, GREEN, ORANGE, TEAL = (
    (235, 90, 80),
    (240, 190, 50),
    (90, 190, 80),
    (245, 140, 40),
    (40, 160, 150),
)


def screen() -> bytes:
    pixels = [[DESKTOP] * WIDTH for _ in range(HEIGHT)]

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

    rect(8, 8, WIDTH - 8, HEIGHT - 8, BAR)
    rect(8, 8, WIDTH - 8, 32, TITLE)
    for n, color in enumerate((RED, AMBER, GREEN)):
        disc(22 + n * 16, 20, 5, color)
    write(16, 44, "FILE  EDIT  VIEW", INK)
    rect(318, 38, 384, 64, GREY)
    write(328, 44, "OPEN", INK)
    rect(394, 38, 464, 64, BLUE)
    write(405, 44, "SAVE", WHITE)
    rect(8, 72, 48, HEIGHT - 8, TOOLS)
    for n in range(6):
        rect(18, 84 + n * 32, 38, 104 + n * 32, GREY)
    rect(56, 72, WIDTH - 16, HEIGHT - 16, CANVAS)
    disc(170, 190, 60, ORANGE)
    rect(260, 130, 400, 270, TEAL)
    return encode(pixels)

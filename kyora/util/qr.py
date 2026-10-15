"""Dependency-free QR Code encoder (ISO/IEC 18004), standard library only.

Scope (enough for Kyora's hub links and LAN URLs):

* byte mode (the text is encoded as UTF-8), versions 1-10, error correction
  levels ``L`` and ``M``;
* Reed-Solomon error correction over GF(256) (primitive polynomial ``0x11D``)
  with generator polynomials ``prod(x - a^i)``, block split and interleaving per
  the version/ECC tables of the standard;
* all eight data masks, scored with the four penalty rules; the lowest score
  wins (ties -> lowest mask number);
* BCH(15,5) format information (generator ``0x537``, XOR mask ``0x5412``) and
  BCH(18,6) version information (generator ``0x1F25``) for versions >= 7.

Public API: :func:`qr_matrix`, :func:`qr_svg`, :func:`qr_ansi`, plus
:func:`encode` (returns a :class:`QRCode` with version/ecc/mask) and the
building blocks :func:`rs_ecc` and :func:`format_bits` used by the tests.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

__all__ = ["QRCode", "encode", "qr_matrix", "qr_svg", "qr_ansi", "rs_ecc", "format_bits", "version_bits",
           "MAX_VERSION", "capacity_bytes"]

MAX_VERSION = 10

# (data codewords, EC codewords per block, [(block count, data codewords per block), ...]) per version, level.
_EC_TABLE: dict[tuple[int, str], tuple[int, int, tuple[tuple[int, int], ...]]] = {
    (1, "L"): (19, 7, ((1, 19),)),
    (1, "M"): (16, 10, ((1, 16),)),
    (2, "L"): (34, 10, ((1, 34),)),
    (2, "M"): (28, 16, ((1, 28),)),
    (3, "L"): (55, 15, ((1, 55),)),
    (3, "M"): (44, 26, ((1, 44),)),
    (4, "L"): (80, 20, ((1, 80),)),
    (4, "M"): (64, 18, ((2, 32),)),
    (5, "L"): (108, 26, ((1, 108),)),
    (5, "M"): (86, 24, ((2, 43),)),
    (6, "L"): (136, 18, ((2, 68),)),
    (6, "M"): (108, 16, ((4, 27),)),
    (7, "L"): (156, 20, ((2, 78),)),
    (7, "M"): (124, 18, ((4, 31),)),
    (8, "L"): (194, 24, ((2, 97),)),
    (8, "M"): (154, 22, ((2, 38), (2, 39))),
    (9, "L"): (232, 30, ((2, 116),)),
    (9, "M"): (182, 22, ((3, 36), (2, 37))),
    (10, "L"): (274, 18, ((2, 68), (2, 69))),
    (10, "M"): (216, 26, ((4, 43), (1, 44))),
}

_ALIGNMENT: dict[int, tuple[int, ...]] = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}

# Format-information ECC indicator bits (ISO/IEC 18004 table 12).
_ECC_FORMAT = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
_SUPPORTED_ECC = ("L", "M")

_PENALTY_N1, _PENALTY_N2, _PENALTY_N3, _PENALTY_N4 = 3, 3, 40, 10


# ----------------------------------------------------------------------------- GF(256) / Reed-Solomon
def _build_gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _build_gf_tables()
_GENERATORS: dict[int, list[int]] = {}


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _generator(degree: int) -> list[int]:
    """Coefficients (highest power first, leading 1) of ``prod_{i<degree} (x - a^i)``."""
    gen = _GENERATORS.get(degree)
    if gen is not None:
        return gen
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, root)
        poly = nxt
    _GENERATORS[degree] = poly
    return poly


def rs_ecc(data: bytes | list[int], degree: int) -> list[int]:
    """Reed-Solomon error-correction codewords: remainder of ``data(x) * x^degree`` by the generator."""
    if not 1 <= degree <= 68:
        raise ValueError("degree out of range")
    gen = _generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = (byte & 0xFF) ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for i in range(degree):
                rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


# ----------------------------------------------------------------------------- BCH codes
def format_bits(ecc: str, mask: int) -> int:
    """15-bit format information for ``ecc`` (L/M/Q/H) and ``mask`` (0-7), BCH-protected and XOR-masked."""
    if ecc not in _ECC_FORMAT or not 0 <= mask <= 7:
        raise ValueError("invalid ecc/mask")
    data = (_ECC_FORMAT[ecc] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412


def version_bits(version: int) -> int:
    """18-bit version information (versions >= 7): 6 version bits + 12 BCH bits."""
    if not 7 <= version <= 40:
        raise ValueError("version information exists only for versions 7-40")
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | (rem & 0xFFF)


# ----------------------------------------------------------------------------- data encoding
def _char_count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def capacity_bytes(version: int, ecc: str) -> int:
    """Maximum UTF-8 byte length that fits byte mode at ``version``/``ecc``."""
    data_cw = _EC_TABLE[(version, ecc)][0]
    return (data_cw * 8 - 4 - _char_count_bits(version)) // 8


def _choose_version(nbytes: int, ecc: str) -> int:
    for v in range(1, MAX_VERSION + 1):
        if nbytes <= capacity_bytes(v, ecc):
            return v
    raise ValueError(f"text too long for a version-{MAX_VERSION} QR code at level {ecc}: {nbytes} bytes "
                     f"(max {capacity_bytes(MAX_VERSION, ecc)})")


def _data_codewords(payload: bytes, version: int, ecc: str) -> list[int]:
    capacity_bits = _EC_TABLE[(version, ecc)][0] * 8
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(payload), _char_count_bits(version))
    for b in payload:
        put(b, 8)
    put(0, min(4, capacity_bits - len(bits)))  # terminator
    put(0, (-len(bits)) % 8)  # byte align
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(out) < capacity_bits // 8:
        out.append(pad[i % 2])
        i += 1
    return out


def _interleave(data: list[int], version: int, ecc: str) -> list[int]:
    _, ec_len, groups = _EC_TABLE[(version, ecc)]
    blocks: list[list[int]] = []
    pos = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[pos:pos + size])
            pos += size
    if pos != len(data):
        raise AssertionError("block table does not match the data length")
    ecs = [rs_ecc(b, ec_len) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_len):
        for e in ecs:
            out.append(e[i])
    return out


# ----------------------------------------------------------------------------- matrix construction
class _Builder:
    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.is_function = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.is_function[y][x] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):  # timing patterns
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        self._finder(3, 3)
        self._finder(size - 4, 3)
        self._finder(3, size - 4)
        pos = _ALIGNMENT[self.version]
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue  # overlaps a finder pattern
                self._alignment(pos[i], pos[j])
        self.draw_format(0)  # reserve the format areas (real bits drawn after masking)
        self._draw_version()

    def _finder(self, cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    dist = max(abs(dx), abs(dy))
                    self.set_fn(x, y, dist not in (2, 4))

    def _alignment(self, cx: int, cy: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set_fn(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(self, bits: int) -> None:
        size = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, size - 15 + i, bit(i))
        self.set_fn(8, size - 8, True)  # the dark module

    def _draw_version(self) -> None:
        if self.version < 7:
            return
        bits = version_bits(self.version)
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a = self.size - 11 + i % 3
            b = i // 3
            self.set_fn(a, b, dark)
            self.set_fn(b, a, dark)

    def draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        total_bits = len(codewords) * 8
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5  # skip the vertical timing column
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    if not self.is_function[y][x] and i < total_bits:
                        self.modules[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2
        if i != total_bits:
            raise AssertionError("codewords do not fit the symbol")

    def apply_mask(self, mask: int) -> None:
        fn = _MASKS[mask]
        for y in range(self.size):
            row = self.modules[y]
            isf = self.is_function[y]
            for x in range(self.size):
                if not isf[x] and fn(x, y):
                    row[x] = not row[x]


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0,
    lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
)

_FINDER_LIKE = ((True, False, True, True, True, False, True, False, False, False, False),
                (False, False, False, False, True, False, True, True, True, False, True))


def penalty_score(matrix: list[list[bool]]) -> int:
    """Total of the four mask-evaluation penalty rules (N1=3, N2=3, N3=40, N4=10)."""
    size = len(matrix)
    score = 0
    lines = [list(r) for r in matrix] + [[matrix[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        # rule 1: runs of >= 5 same-colour modules
        run_color = line[0]
        run_len = 1
        for v in line[1:]:
            if v == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += _PENALTY_N1 + run_len - 5
                run_color = v
                run_len = 1
        if run_len >= 5:
            score += _PENALTY_N1 + run_len - 5
        # rule 3: 1:1:3:1:1 finder-like pattern with 4 light modules on one side
        for i in range(size - 10):
            window = tuple(line[i:i + 11])
            if window == _FINDER_LIKE[0] or window == _FINDER_LIKE[1]:
                score += _PENALTY_N3
    # rule 2: 2x2 blocks of one colour
    for y in range(size - 1):
        r0, r1 = matrix[y], matrix[y + 1]
        for x in range(size - 1):
            c = r0[x]
            if c == r0[x + 1] == r1[x] == r1[x + 1]:
                score += _PENALTY_N2
    # rule 4: dark proportion deviation from 50 %, per 5 % step
    dark = sum(sum(1 for v in row if v) for row in matrix)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    score += max(k, 0) * _PENALTY_N4
    return score


# ----------------------------------------------------------------------------- public API
@dataclass(frozen=True)
class QRCode:
    """An encoded symbol: ``modules[y][x]`` is True for a dark module (no quiet zone)."""
    version: int
    ecc: str
    mask: int
    modules: tuple[tuple[bool, ...], ...]

    @property
    def size(self) -> int:
        return len(self.modules)


def _normalise_ecc(ecc: str) -> str:
    level = str(ecc or "M").upper()
    if level not in _SUPPORTED_ECC:
        raise ValueError(f"error correction level must be one of {_SUPPORTED_ECC}, got {ecc!r}")
    return level


def encode(text: str | bytes, ecc: str = "M", *, version: int | None = None, mask: int | None = None) -> QRCode:
    """Encode ``text`` (UTF-8, byte mode) into the smallest version 1-10 that fits.

    ``version`` forces a minimum version, ``mask`` forces a mask (otherwise the
    lowest-penalty mask is chosen). Raises ``ValueError`` when the text does
    not fit version 10.
    """
    level = _normalise_ecc(ecc)
    payload = text if isinstance(text, (bytes, bytearray)) else str(text).encode("utf-8")
    ver = _choose_version(len(payload), level)
    if version is not None:
        if not 1 <= version <= MAX_VERSION:
            raise ValueError(f"version must be 1-{MAX_VERSION}")
        if version < ver:
            raise ValueError(f"text needs at least version {ver}")
        ver = version
    if mask is not None and not 0 <= mask <= 7:
        raise ValueError("mask must be 0-7")
    codewords = _interleave(_data_codewords(bytes(payload), ver, level), ver, level)
    builder = _Builder(ver)
    builder.draw_function_patterns()
    builder.draw_codewords(codewords)
    base = [row[:] for row in builder.modules]

    best_mask, best_score, best_modules = -1, None, None
    for m in ([mask] if mask is not None else range(8)):
        builder.modules = [row[:] for row in base]
        builder.apply_mask(m)
        builder.draw_format(format_bits(level, m))
        score = penalty_score(builder.modules)
        if best_score is None or score < best_score:
            best_mask, best_score, best_modules = m, score, [row[:] for row in builder.modules]
    assert best_modules is not None
    return QRCode(version=ver, ecc=level, mask=best_mask, modules=tuple(tuple(r) for r in best_modules))


def qr_matrix(text: str, ecc: str = "M") -> list[list[bool]]:
    """Return the module matrix (``True`` = dark), ``(17 + 4*version)`` square, without quiet zone."""
    return [list(row) for row in encode(text, ecc).modules]


def qr_svg(text: str, scale: int = 6, margin: int = 4, ecc: str = "M") -> str:
    """Render ``text`` as a standalone SVG: white background plus one ``<path>`` of dark modules."""
    if scale < 1 or margin < 0:
        raise ValueError("scale must be >= 1 and margin >= 0")
    matrix = encode(text, ecc).modules
    n = len(matrix)
    dim = n + 2 * margin
    parts: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{start + margin},{y + margin}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    px = dim * scale
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" viewBox="0 0 {dim} {dim}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
            f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
            f'<path fill="#000000" d="{"".join(parts)}"/></svg>')


def _stdout_encoding() -> str:
    return (getattr(sys.stdout, "encoding", None) or "").lower().replace("_", "-")


def _ansi_colour_supported() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        env = os.environ
        return bool(env.get("WT_SESSION") or env.get("ANSICON") or env.get("ConEmuANSI") == "ON"
                    or env.get("TERM_PROGRAM") or env.get("TERM"))
    return True


def qr_ansi(text: str, ecc: str = "L", *, margin: int = 2, color: bool | None = None,
            encoding: str | None = None) -> str:
    """Render ``text`` for a terminal.

    UTF-8 terminals get two module rows per line with half-block characters
    (``▀ ▄ █``); otherwise each module is two characters (``##`` blocks). As with
    ``qrencode -t UTF8``, *light* modules are drawn so the code reads correctly on
    the usual dark terminal background; when ANSI colour is available (default:
    auto-detected, ``color`` overrides) the colours are pinned to white on black
    so light-themed terminals render it correctly too.
    """
    matrix = encode(text, ecc).modules
    n = len(matrix)
    use_color = _ansi_colour_supported() if color is None else bool(color)
    enc = (encoding or _stdout_encoding()).lower().replace("_", "-")
    utf8 = enc in ("utf-8", "utf8")
    dim = n + 2 * margin

    def dark(x: int, y: int) -> bool:
        xx, yy = x - margin, y - margin
        return 0 <= xx < n and 0 <= yy < n and matrix[yy][xx]

    lines: list[str] = []
    if utf8:
        prefix, suffix = ("\x1b[97;40m", "\x1b[0m") if use_color else ("", "")
        for y in range(0, dim, 2):
            chars = []
            for x in range(dim):
                top_light = not dark(x, y)
                bottom_light = (y + 1 < dim) and not dark(x, y + 1)
                if y + 1 >= dim:
                    bottom_light = False  # nothing below the last row: leave it blank
                chars.append("█" if top_light and bottom_light else "▀" if top_light else "▄" if bottom_light else " ")
            lines.append(prefix + "".join(chars) + suffix)
    else:
        for y in range(dim):
            if use_color:
                row = "".join("\x1b[40m  " if dark(x, y) else "\x1b[107m  " for x in range(dim))
                lines.append(row + "\x1b[0m")
            else:
                lines.append("".join("  " if dark(x, y) else "##" for x in range(dim)))
    return "\n".join(lines)

"""QR encoder tests: structure, published vectors, an independent in-test decoder, and real decoders.

Real third-party decoding (sub-test ``test_real_decoder_*``):
* headless Chromium via Node + Playwright renders the SVG to a canvas and decodes it
  with the built-in ``BarcodeDetector`` when present (not on Linux desktop builds),
  otherwise with jsQR when it is resolvable (``KYORA_JSQR_PATH`` = jsqr package dir or
  ``dist/jsQR.js``, or ``require('jsqr')`` via ``NODE_PATH``);
* the ``zxingcpp`` Python package when it is installed (needs numpy).
Nothing is downloaded by the tests; unavailable decoders are skipped with a reason.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kyora.util.qr import (QRCode, capacity_bytes, encode, format_bits, penalty_score, qr_ansi, qr_matrix, qr_svg,
                           rs_ecc, version_bits)

# ----------------------------------------------------------------------------- independent reference data
# ISO/IEC 18004 / Thonky format information strings (ECC level, mask) -> 15 bits, MSB first.
FORMAT_TABLE = {
    ("L", 0): "111011111000100", ("L", 1): "111001011110011", ("L", 2): "111110110101010", ("L", 3): "111100010011101",
    ("L", 4): "110011000101111", ("L", 5): "110001100011000", ("L", 6): "110110001000001", ("L", 7): "110100101110110",
    ("M", 0): "101010000010010", ("M", 1): "101000100100101", ("M", 2): "101111001111100", ("M", 3): "101101101001011",
    ("M", 4): "100010111111001", ("M", 5): "100000011001110", ("M", 6): "100111110010111", ("M", 7): "100101010100000",
}
VERSION_TABLE = {7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3}
ALIGNMENT_CENTERS = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
                     7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
# version-ecc: (ec codewords per block, [(blocks, data codewords per block), ...])
BLOCKS = {
    (1, "L"): (7, [(1, 19)]), (1, "M"): (10, [(1, 16)]), (2, "L"): (10, [(1, 34)]), (2, "M"): (16, [(1, 28)]),
    (3, "L"): (15, [(1, 55)]), (3, "M"): (26, [(1, 44)]), (4, "L"): (20, [(1, 80)]), (4, "M"): (18, [(2, 32)]),
    (5, "L"): (26, [(1, 108)]), (5, "M"): (24, [(2, 43)]), (6, "L"): (18, [(2, 68)]), (6, "M"): (16, [(4, 27)]),
    (7, "L"): (20, [(2, 78)]), (7, "M"): (18, [(4, 31)]), (8, "L"): (24, [(2, 97)]), (8, "M"): (22, [(2, 38), (2, 39)]),
    (9, "L"): (30, [(2, 116)]), (9, "M"): (22, [(3, 36), (2, 37)]),
    (10, "L"): (18, [(2, 68), (2, 69)]), (10, "M"): (26, [(4, 43), (1, 44)]),
}
ECC_FROM_BITS = {0b01: "L", 0b00: "M", 0b11: "Q", 0b10: "H"}

# ----------------------------------------------------------------------------- independent GF(256) for syndromes
_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v = (_v << 1) ^ (0x11D if _v & 0x80 else 0)
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _bch_format(data5: int) -> int:
    """Independent BCH(15,5): data * x^10 mod 10100110111, then XOR 101010000010010."""
    value = data5 << 10
    for shift in range(14, 9, -1):
        if value & (1 << shift):
            value ^= 0b10100110111 << (shift - 10)
    return ((data5 << 10) | value) ^ 0b101010000010010


VALID_FORMATS = {_bch_format(d): d for d in range(32)}


def _mask_fn(mask: int):
    return [lambda i, j: (i + j) % 2 == 0, lambda i, j: i % 2 == 0, lambda i, j: j % 3 == 0,
            lambda i, j: (i + j) % 3 == 0, lambda i, j: (i // 2 + j // 3) % 2 == 0,
            lambda i, j: (i * j) % 2 + (i * j) % 3 == 0, lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
            lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0][mask]


# ----------------------------------------------------------------------------- in-test decoder
class DecodeError(AssertionError):
    pass


def _function_map(version: int) -> list[list[bool]]:
    size = 17 + 4 * version
    f = [[False] * size for _ in range(size)]

    def mark(col0: int, row0: int, w: int, h: int) -> None:
        for r in range(row0, row0 + h):
            for c in range(col0, col0 + w):
                if 0 <= r < size and 0 <= c < size:
                    f[r][c] = True

    mark(0, 0, 9, 9)
    mark(size - 8, 0, 8, 9)
    mark(0, size - 8, 9, 8)
    for i in range(size):
        f[6][i] = f[i][6] = True
    centers = ALIGNMENT_CENTERS[version]
    for r in centers:
        for c in centers:
            if f[r][c]:
                continue  # overlaps a finder
            mark(c - 2, r - 2, 5, 5)
    if version >= 7:
        mark(size - 11, 0, 3, 6)
        mark(0, size - 11, 6, 3)
    return f


def read_format(m: list[list[bool]]) -> tuple[int, int]:
    """Return both 15-bit format copies (MSB first, coordinates from the spec figure)."""
    size = len(m)
    first = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
             (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    second = [(size - 1 - i, 8) for i in range(7)] + [(8, size - 8 + i) for i in range(8)]
    vals = []
    for coords in (first, second):
        v = 0
        for r, c in coords:
            v = (v << 1) | (1 if m[r][c] else 0)
        vals.append(v)
    return vals[0], vals[1]


def read_version(m: list[list[bool]]) -> tuple[int, int]:
    size = len(m)
    top_right = bottom_left = 0
    for i in range(18):
        if m[i // 3][size - 11 + i % 3]:
            top_right |= 1 << i
        if m[size - 11 + i % 3][i // 3]:
            bottom_left |= 1 << i
    return top_right, bottom_left


def decode_matrix(m: list[list[bool]]) -> dict:
    """Decode a clean byte-mode symbol (v1-10): format, unmask, de-interleave, RS syndromes, payload."""
    size = len(m)
    if (size - 17) % 4 or not 21 <= size <= 57:
        raise DecodeError(f"bad size {size}")
    version = (size - 17) // 4
    f1, f2 = read_format(m)
    if f1 != f2 or f1 not in VALID_FORMATS:
        raise DecodeError(f"format copies {f1:015b}/{f2:015b} are not a valid codeword")
    data5 = VALID_FORMATS[f1]
    ecc, mask = ECC_FROM_BITS[data5 >> 3], data5 & 7
    if version >= 7:
        tr, bl = read_version(m)
        if tr != bl or tr != VERSION_TABLE[version]:
            raise DecodeError(f"version info {tr:018b}/{bl:018b}")
    func = _function_map(version)
    mfn = _mask_fn(mask)
    bits: list[int] = []
    col, upward = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not func[r][c]:
                    bits.append(int(m[r][c] ^ mfn(r, c)))
        upward = not upward
        col -= 2
    ec_len, groups = BLOCKS[(version, ecc)]
    lens = [n for count, n in groups for _ in range(count)]
    total = sum(lens) + ec_len * len(lens)
    if len(bits) < total * 8:
        raise DecodeError("not enough data modules")
    cws = [int("".join(map(str, bits[i * 8:i * 8 + 8])), 2) for i in range(total)]
    data_blocks = [[] for _ in lens]
    pos = 0
    for i in range(max(lens)):
        for b, n in enumerate(lens):
            if i < n:
                data_blocks[b].append(cws[pos])
                pos += 1
    ec_blocks = [[] for _ in lens]
    for _ in range(ec_len):
        for b in range(len(lens)):
            ec_blocks[b].append(cws[pos])
            pos += 1
    for b in range(len(lens)):
        word = data_blocks[b] + ec_blocks[b]
        for k in range(ec_len):
            acc = 0
            for coef in word:
                acc = _mul(acc, _EXP[k]) ^ coef
            if acc:
                raise DecodeError(f"block {b} syndrome {k} != 0")
    data = [x for blk in data_blocks for x in blk]
    stream = "".join(f"{x:08b}" for x in data)
    if stream[:4] != "0100":
        raise DecodeError("not byte mode")
    cc = 8 if version <= 9 else 16
    count = int(stream[4:4 + cc], 2)
    start = 4 + cc
    payload = bytes(int(stream[start + 8 * k:start + 8 * k + 8], 2) for k in range(count))
    end = start + 8 * count
    term = stream[end:end + 4]
    if term.strip("0"):
        raise DecodeError("terminator bits are not zero")
    pad_start = (end + min(4, len(stream) - end) + 7) // 8
    pads = data[pad_start:]
    if any(p != (0xEC, 0x11)[i % 2] for i, p in enumerate(pads)):
        raise DecodeError("pad codewords are not EC 11 EC 11 ...")
    return {"version": version, "ecc": ecc, "mask": mask, "text": payload.decode("utf-8")}


# ----------------------------------------------------------------------------- tests
SAMPLES = [
    ("HELLO WORLD", "M"),
    ("https://example.com/", "L"),
    ("http://127.0.0.1:8765/#hub=http%3A%2F%2F192.168.100.200%3A8765&token=Zr2w-9_kQx7bLm3nP0aYt5uVcE1dH8fJ", "L"),
    ("http://127.0.0.1:8765/#hub=http%3A%2F%2F192.168.100.200%3A8765&token=Zr2w-9_kQx7bLm3nP0aYt5uVcE1dH8fJ", "M"),
    ("Kyora · ゲーム · ✓", "M"),
    ("x" * capacity_bytes(7, "M"), "M"),
    ("y" * capacity_bytes(9, "L"), "L"),
    ("z" * capacity_bytes(10, "M"), "M"),
    ("w" * capacity_bytes(10, "L"), "L"),
]


class StructureTests(unittest.TestCase):
    def test_reed_solomon_thonky_hello_world_1m(self):
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]  # 0x20 0x5B 0x0B ...
        self.assertEqual(rs_ecc(data, 10), [0xC4, 0x23, 0x27, 0x77, 0xEB, 0xD7, 0xE7, 0xE2, 0x5D, 0x17])

    def test_format_and_version_codes_match_published_tables(self):
        for (ecc, mask), bits in FORMAT_TABLE.items():
            self.assertEqual(format_bits(ecc, mask), int(bits, 2), (ecc, mask))
            self.assertEqual(_bch_format(int(bits, 2) >> 10 ^ 0b10101), int(bits, 2))
        for v, bits in VERSION_TABLE.items():
            self.assertEqual(version_bits(v), bits)

    def test_structure_and_independent_decode(self):
        for text, ecc in SAMPLES:
            with self.subTest(text=text[:30], ecc=ecc):
                qr = encode(text, ecc)
                m = qr_matrix(text, ecc)
                size = len(m)
                self.assertEqual(size, 17 + 4 * qr.version)
                self.assertTrue(all(len(row) == size for row in m))
                self.assertEqual(m, [list(r) for r in qr.modules])
                for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
                    for dr in range(7):
                        for dc in range(7):
                            ring = max(abs(dr - 3), abs(dc - 3))
                            self.assertEqual(m[r0 + dr][c0 + dc], ring != 2, ("finder", r0, c0, dr, dc))
                for i in range(8):  # separators
                    self.assertFalse(m[7][i] or m[i][7] or m[7][size - 1 - i] or m[i][size - 8]
                                     or m[size - 8][i] or m[size - 1 - i][7])
                for i in range(8, size - 8):  # timing
                    self.assertEqual(m[6][i], i % 2 == 0)
                    self.assertEqual(m[i][6], i % 2 == 0)
                self.assertTrue(m[4 * qr.version + 9][8], "dark module")
                centers = ALIGNMENT_CENTERS[qr.version]
                for r in centers:
                    for c in centers:
                        if (r, c) in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                            continue
                        for dr in range(-2, 3):
                            for dc in range(-2, 3):
                                self.assertEqual(m[r + dr][c + dc], max(abs(dr), abs(dc)) != 1, ("align", r, c))
                f1, f2 = read_format(m)
                self.assertEqual(f1, f2)
                self.assertEqual(f"{f1:015b}", FORMAT_TABLE[(ecc, qr.mask)])
                decoded = decode_matrix(m)
                self.assertEqual(decoded, {"version": qr.version, "ecc": ecc, "mask": qr.mask, "text": text})

    def test_every_mask_decodes_and_best_mask_is_chosen(self):
        text = "mask test " * 9
        scores = {}
        for mask in range(8):
            q = encode(text, "M", mask=mask)
            self.assertEqual(decode_matrix([list(r) for r in q.modules])["mask"], mask)
            self.assertEqual(decode_matrix([list(r) for r in q.modules])["text"], text)
            scores[mask] = penalty_score([list(r) for r in q.modules])
        auto = encode(text, "M")
        self.assertEqual(auto.mask, min(scores, key=lambda k: (scores[k], k)))

    def test_version_selection_and_limits(self):
        self.assertEqual(capacity_bytes(1, "L"), 17)
        self.assertEqual(capacity_bytes(1, "M"), 14)
        self.assertEqual(capacity_bytes(10, "M"), 213)
        self.assertEqual(capacity_bytes(10, "L"), 271)
        self.assertEqual(encode("a" * 17, "L").version, 1)
        self.assertEqual(encode("a" * 18, "L").version, 2)
        self.assertEqual(encode("é" * 7, "M").version, 1)  # 14 UTF-8 bytes
        self.assertEqual(encode("é" * 8, "M").version, 2)
        self.assertEqual(encode("a", "M", version=10).version, 10)
        self.assertEqual(decode_matrix([list(r) for r in encode("a", "M", version=10).modules])["text"], "a")
        with self.assertRaises(ValueError):
            encode("a" * 214, "M")
        with self.assertRaises(ValueError):
            encode("abc", "Q")
        with self.assertRaises(ValueError):
            encode("abc", "M", mask=8)
        self.assertIsInstance(encode("abc"), QRCode)


class RenderTests(unittest.TestCase):
    TEXT = "http://192.168.1.20:8765/"

    def test_svg_single_path_matches_matrix(self):
        svg = qr_svg(self.TEXT, scale=5, margin=4, ecc="M")
        m = qr_matrix(self.TEXT, "M")
        n = len(m)
        dim = n + 8
        self.assertEqual(svg.count("<path"), 1)
        self.assertIn('shape-rendering="crispEdges"', svg)
        self.assertIn(f'viewBox="0 0 {dim} {dim}"', svg)
        self.assertIn(f'width="{dim * 5}"', svg)
        d = re.search(r'<path[^>]* d="([^"]+)"', svg).group(1)
        grid = [[False] * n for _ in range(n)]
        for x, y, w in re.findall(r"M(\d+),(\d+)h(\d+)v1h-\d+z", d):
            for k in range(int(w)):
                grid[int(y) - 4][int(x) - 4 + k] = True
        self.assertEqual(grid, m)

    def test_ansi_half_blocks_roundtrip(self):
        out = qr_ansi(self.TEXT, ecc="L", margin=2, color=False, encoding="utf-8")
        m = qr_matrix(self.TEXT, "L")
        n = len(m)
        dim = n + 4
        lines = out.split("\n")
        self.assertEqual(len(lines), (dim + 1) // 2)
        self.assertTrue(all(len(line) == dim for line in lines))
        light = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}
        for li, line in enumerate(lines):
            for x, ch in enumerate(line):
                top, bottom = light[ch]
                for y, is_light in ((2 * li, top), (2 * li + 1, bottom)):
                    if y >= dim:
                        continue
                    xx, yy = x - 2, y - 2
                    expected_dark = 0 <= xx < n and 0 <= yy < n and m[yy][xx]
                    self.assertEqual(not is_light, expected_dark, (x, y))

    def test_ansi_fallbacks(self):
        plain = qr_ansi("abc", ecc="L", margin=2, color=False, encoding="cp1252")
        lines = plain.split("\n")
        self.assertEqual(len(lines), 21 + 4)
        self.assertTrue(all(len(line) == 2 * 25 and set(line) <= {"#", " "} for line in lines))
        self.assertTrue(lines[0] == "##" * 25)
        coloured = qr_ansi("abc", ecc="L", color=True, encoding="ascii")
        self.assertIn("\x1b[40m", coloured)
        self.assertNotIn("█", coloured)
        self.assertIn("\x1b[97;40m", qr_ansi("abc", color=True, encoding="UTF-8"))


def _node_env() -> dict | None:
    node = shutil.which("node")
    if not node:
        return None
    env = dict(os.environ)
    if not env.get("NODE_PATH"):
        npm = shutil.which("npm")
        if npm:
            try:
                root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
                if root:
                    env["NODE_PATH"] = root
            except (OSError, subprocess.SubprocessError):
                pass
    if not env.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/opt/pw-browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    return env


def _node_resolve(env: dict, module: str) -> str | None:
    try:
        proc = subprocess.run(["node", "-e", f"process.stdout.write(require.resolve({json.dumps(module)}))"],
                              capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _jsqr_path(env: dict) -> str | None:
    explicit = os.environ.get("KYORA_JSQR_PATH")
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            p = p / "dist" / "jsQR.js"
        return str(p) if p.is_file() else None
    return _node_resolve(env, "jsqr")


_BROWSER_SCRIPT = r"""
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const jsqr = process.argv[3] || '';
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.setContent('<!doctype html><html><body></body></html>');
    if (jsqr) await page.addScriptTag({ path: jsqr });
    const out = [];
    for (const svg of cases) {
      out.push(await page.evaluate(async (svgText) => {
        const img = new Image();
        img.src = 'data:image/svg+xml;base64,' + btoa(svgText);
        await img.decode();
        const cv = document.createElement('canvas');
        cv.width = img.naturalWidth; cv.height = img.naturalHeight;
        const ctx = cv.getContext('2d');
        ctx.drawImage(img, 0, 0);
        if ('BarcodeDetector' in window) {
          const formats = await BarcodeDetector.getSupportedFormats();
          if (formats.includes('qr_code')) {
            const found = await new BarcodeDetector({ formats: ['qr_code'] }).detect(cv);
            return { method: 'BarcodeDetector', text: found.length ? found[0].rawValue : null };
          }
        }
        if (window.jsQR) {
          const d = ctx.getImageData(0, 0, cv.width, cv.height);
          const r = window.jsQR(d.data, cv.width, cv.height);
          return { method: 'jsQR', text: r ? new TextDecoder().decode(new Uint8Array(r.binaryData)) : null };
        }
        return { method: null, text: null };
      }, svg));
    }
    process.stdout.write(JSON.stringify(out));
  } finally {
    await browser.close();
  }
})().catch((e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(2); });
"""


class RealDecoderTests(unittest.TestCase):
    TEXTS = [(t, e) for t, e in SAMPLES if len(t) < 120] + [("w" * capacity_bytes(10, "L"), "L")]

    def test_real_decoder_headless_chromium(self):
        env = _node_env()
        if env is None:
            self.skipTest("node not installed")
        if not _node_resolve(env, "playwright"):
            self.skipTest("playwright not resolvable from node")
        jsqr = _jsqr_path(env) or ""
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "decode.cjs"
            script.write_text(_BROWSER_SCRIPT, encoding="utf-8")
            cases = Path(d) / "cases.json"
            cases.write_text(json.dumps([qr_svg(t, scale=4, margin=4, ecc=e) for t, e in self.TEXTS]), encoding="utf-8")
            proc = subprocess.run(["node", str(script), str(cases), jsqr], capture_output=True, text=True,
                                  timeout=120, env=env, cwd=d)
        if proc.returncode != 0:
            self.skipTest(f"headless chromium unavailable: {proc.stderr.strip()[-300:]}")
        results = json.loads(proc.stdout)
        if all(r["method"] is None for r in results):
            self.skipTest("no real decoder in the browser: BarcodeDetector is not available in this Chromium build "
                          "and jsQR is not installed (set KYORA_JSQR_PATH to a jsqr package to enable)")
        for (text, ecc), res in zip(self.TEXTS, results):
            with self.subTest(method=res["method"], ecc=ecc, text=text[:30]):
                self.assertEqual(res["text"], text)

    def test_real_decoder_zxingcpp(self):
        try:
            import numpy as np  # noqa: F401
            import zxingcpp  # type: ignore
        except ImportError:
            self.skipTest("zxingcpp/numpy not installed (tests download nothing)")
        for text, ecc in self.TEXTS:
            m = qr_matrix(text, ecc)
            n, margin, scale = len(m), 4, 4
            dim = (n + 2 * margin) * scale
            img = np.full((dim, dim), 255, dtype=np.uint8)
            for y in range(n):
                for x in range(n):
                    if m[y][x]:
                        img[(y + margin) * scale:(y + margin + 1) * scale, (x + margin) * scale:(x + margin + 1) * scale] = 0
            found = zxingcpp.read_barcodes(img)
            with self.subTest(text=text[:30], ecc=ecc):
                self.assertTrue(found)
                self.assertEqual(found[0].text, text)


if __name__ == "__main__":
    unittest.main()

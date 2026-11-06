"""Guard: no code in this repository may place, modify or cancel an order (KITE_SETUP.md, hard rule).

Scans every .py file under papertrade/ and tools/ and fails if one of the order method names below appears
anywhere outside a comment: in code, in a string or in a docstring (a docstring that names one is easy to
turn into a call, and an f-string or getattr() could call one by name). Comments are allowed so that a
line can say what is forbidden. This file itself lives in tests/ and is not scanned.

Run:  python -m unittest discover -s tests      or      python tests/test_no_orders.py
"""
import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("papertrade", "tools")
FORBIDDEN = ("place_order", "modify_order", "cancel_order", "place_gtt", "modify_gtt", "convert_position",
             "place_mf_order")
PATTERN = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(FORBIDDEN) + r")(?![A-Za-z0-9_])")


def offences(path: Path) -> list[str]:
    out = []
    src = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        for m in PATTERN.finditer(tok.string):
            out.append(f"{path.relative_to(ROOT)}:{tok.start[0]}: {m.group(1)}")
    return out


class NoOrderCalls(unittest.TestCase):
    def test_scanned_folders_exist(self):
        for d in SCANNED:
            self.assertTrue((ROOT / d).is_dir(), f"{d}/ is missing")

    def test_no_order_method_names(self):
        files = [p for d in SCANNED for p in sorted((ROOT / d).rglob("*.py"))]
        self.assertGreater(len(files), 0)
        found = [o for p in files for o in offences(p)]
        self.assertEqual(found, [], "order method names found outside comments:\n" + "\n".join(found))

    def test_the_scanner_catches_a_call(self):
        """The pattern itself works: a call, a string and a docstring are caught, a comment is not."""
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            p = Path(d) / "probe.py"
            p.write_text('"""uses cancel_order"""\n# place_order is fine in a comment\nk.place_order(x)\n'
                         'getattr(k, "modify_gtt")\nplace_orders_total = 1\n', encoding="utf-8")
            names = [o.rsplit(": ", 1)[1] for o in offences(p)]
        self.assertEqual(names, ["cancel_order", "place_order", "modify_gtt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Make the repository's delivery folders importable from the tests."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DELIVERY = ROOT / "支付对账与差错追踪系统_交付"

for path in (ROOT, DELIVERY, DELIVERY / "webapp"):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

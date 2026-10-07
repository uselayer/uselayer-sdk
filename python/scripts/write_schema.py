"""Write schema/order.json from the Order model: python scripts/write_schema.py"""

import json
from pathlib import Path

from uselayer.orders import order_schema

out = Path(__file__).resolve().parents[2] / "schema" / "order.json"
out.write_text(json.dumps(order_schema(), indent=2) + "\n")
print(f"✓ wrote {out}")

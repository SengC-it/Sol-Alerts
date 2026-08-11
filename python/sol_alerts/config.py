from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or ROOT / "config" / "strategy-v1.json"
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if value.get("configVersion", "").startswith("1.0.") is False:
        raise ValueError("unsupported configVersion")
    if value.get("schemaVersion") != 1:
        raise ValueError("unsupported schemaVersion")
    if value["universe"]["quoteAsset"] != "USDT" or value["universe"]["contractType"] != "PERPETUAL":
        raise ValueError("research package only supports USDT perpetuals")
    if value["universe"]["minQuoteVolume24h"] != 20_000_000:
        raise ValueError("frozen v1 requires a 20,000,000 USDT 24h quote-volume gate")
    fractions = [value["research"][key] for key in ("trainFraction", "validationFraction", "testFraction")]
    if abs(sum(fractions) - 1.0) > 1e-12:
        raise ValueError("ordered split fractions must sum to one")
    base = value["weights"]["globalBase"]
    if abs(sum(base.values()) - 1.0) > 1e-12:
        raise ValueError("global weights must sum to one")
    return value

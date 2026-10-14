from __future__ import annotations

from pathlib import Path

import pandas as pd


DEFAULT_POOL = Path(__file__).resolve().parent.parent / "data" / "stock_pool.csv"


def load_universe(path: str | Path | None = None) -> pd.DataFrame:
    """Load and validate a unique Futu-format stock universe."""
    source = Path(path) if path else DEFAULT_POOL
    pool = pd.read_csv(source, dtype={"code": "string", "name": "string"})
    required = {"code", "name"}
    if not required.issubset(pool.columns):
        raise ValueError(f"股票池必须包含列: {sorted(required)}")
    pool = pool.loc[:, ["code", "name"]].dropna().copy()
    pool["code"] = pool["code"].str.strip().str.upper()
    pool["name"] = pool["name"].str.strip()
    bad = ~pool["code"].str.match(r"^(SH|SZ)\.\d{6}$")
    if bad.any():
        raise ValueError(f"股票代码格式错误: {pool.loc[bad, 'code'].tolist()}")
    return pool.drop_duplicates("code", keep="first").reset_index(drop=True)

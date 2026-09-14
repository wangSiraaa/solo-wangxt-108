"""应用配置：通过环境变量覆盖，默认连接本机用户态 PostgreSQL。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    pg_dsn: str = os.getenv(
        "ROAST_PG_DSN",
        "host=/tmp port=5433 dbname=roastdb user=roast connect_timeout=5",
    )
    # 计算参数默认值（导出时也会显式写入，保证可复现）
    default_smooth_window_s: float = float(os.getenv("ROAST_SMOOTH_WINDOW_S", "30"))
    default_ror_window_s: float = float(os.getenv("ROAST_ROR_WINDOW_S", "45"))
    # 缺测超过该秒数的内部缺口，不做线性插值（只标注，不冒充数据）
    max_interp_gap_s: float = float(os.getenv("ROAST_MAX_INTERP_GAP_S", "20"))


settings = Settings()

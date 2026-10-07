from pydantic import BaseModel

class RiskSummary(BaseModel):
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_utilization_pct: float  # Margin / Equity * 100
    margin_level_pct: float | None  # 참고용: MT5 기본 제공 값 (Equity / Margin * 100)
    real_leverage: float
    nominal_leverage: int
    risk_level: str  # "safe" | "warning" | "danger"
    profit: float
 
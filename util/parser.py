from datetime import datetime

def parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # 'Z' suffix 대응 (Python 3.10 이하 fromisoformat 호환)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    raise ValueError(f"Unexpected datetime type: {type(value)}")
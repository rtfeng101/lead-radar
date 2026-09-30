"""Match % -> priority band. The UI mirrors these two numbers (frontend/src/components/LeadTable.tsx)."""

HOT_MIN = 75
WARM_MIN = 50


def band(match_pct: int) -> str:
    return "hot" if match_pct >= HOT_MIN else "warm" if match_pct >= WARM_MIN else "cold"

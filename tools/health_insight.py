import sqlite3
from typing import Any, Dict, List, Optional


TOOL_DEF = {
    "name": "health_insight",
    "description": (
        "최근 러닝 데이터를 기반으로 추세와 목표 대비 상태를 해석합니다.\n"
        "- pace/distance/weekly_mileage/recovery 관점의 핵심 인사이트 제공\n"
        "- 최근 n주 추세, 목표 대비 gap, 급격한 주간 증가 여부를 계산"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["pace", "distance", "weekly_mileage", "recovery"],
                "description": "분석할 지표",
            },
            "weeks": {
                "type": "integer",
                "minimum": 2,
                "maximum": 12,
                "default": 4,
                "description": "최근 n주 분석",
            },
        },
        "required": ["metric"],
    },
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _trend_direction(values: List[float], lower_is_better: bool) -> str:
    if len(values) < 2:
        return "insufficient_data"
    delta = values[0] - values[-1]
    if abs(delta) < 0.05:
        return "stable"
    if lower_is_better:
        return "improving" if delta < 0 else "worsening"
    return "improving" if delta > 0 else "worsening"


def _build_pace_insight(series: List[Dict[str, Any]], user: Dict[str, Any], guidelines: Dict[str, List[str]]) -> Dict[str, Any]:
    pace_values = [_safe_float(row.get("avg_pace")) for row in series]
    pace_values = [value for value in pace_values if value is not None]
    target_pace = _safe_float(user.get("target_pace_min_km"))
    latest_pace = pace_values[0] if pace_values else None

    gap = None
    if latest_pace is not None and target_pace is not None:
        gap = round(latest_pace - target_pace, 3)

    return {
        "metric": "pace",
        "latest_value": latest_pace,
        "target_value": target_pace,
        "gap_to_target": gap,
        "trend": _trend_direction(pace_values, lower_is_better=True),
        "insight": (
            "목표 페이스보다 느립니다." if gap is not None and gap > 0.1
            else "목표 페이스 범위에 근접했습니다." if gap is not None
            else "페이스 비교를 위한 데이터가 부족합니다."
        ),
        "guidelines": guidelines.get("pace", []),
    }


def _build_distance_insight(series: List[Dict[str, Any]], user: Dict[str, Any]) -> Dict[str, Any]:
    totals = [_safe_float(row.get("total_km")) for row in series]
    totals = [value for value in totals if value is not None]
    latest = totals[0] if totals else None
    previous = totals[1] if len(totals) > 1 else None
    growth = round(latest - previous, 2) if latest is not None and previous is not None else None

    return {
        "metric": "distance",
        "latest_value": latest,
        "previous_value": previous,
        "week_over_week_change": growth,
        "trend": _trend_direction(totals, lower_is_better=False),
        "insight": (
            "주간 거리가 급증했습니다. 회복 상태를 함께 확인하는 편이 좋습니다."
            if growth is not None and growth >= 10
            else "주간 거리 변화가 안정적입니다."
        ),
        "target_value": _safe_float(user.get("weekly_target_km")),
    }


def _build_weekly_mileage_insight(series: List[Dict[str, Any]], user: Dict[str, Any]) -> Dict[str, Any]:
    totals = [_safe_float(row.get("total_km")) for row in series]
    totals = [value for value in totals if value is not None]
    latest = totals[0] if totals else None
    target = _safe_float(user.get("weekly_target_km"))
    gap = round(latest - target, 2) if latest is not None and target is not None else None

    return {
        "metric": "weekly_mileage",
        "latest_value": latest,
        "target_value": target,
        "gap_to_target": gap,
        "trend": _trend_direction(totals, lower_is_better=False),
        "insight": (
            "주간 목표 거리를 달성했습니다." if gap is not None and gap >= 0
            else "주간 목표 거리까지 추가 러닝이 필요합니다." if gap is not None
            else "주간 거리 비교를 위한 데이터가 부족합니다."
        ),
    }


def _build_recovery_signal_detail(signals: Dict[str, Any]) -> Dict[str, Any]:
    load_ratio = signals.get("load_ratio")
    avg_rest_days = signals.get("avg_rest_days")
    avg_bpm = signals.get("avg_bpm")
    baseline_hr = _safe_float(signals.get("resting_heart_rate"))
    hr_gap = round(avg_bpm - baseline_hr, 1) if avg_bpm is not None and baseline_hr is not None else None

    cautions: List[str] = []
    if load_ratio is not None and load_ratio >= 1.2:
        cautions.append("주간 러닝 부하가 직전 주 대비 많이 증가했습니다.")
    if avg_rest_days is not None and avg_rest_days < 1.0:
        cautions.append("세션 간 휴식 간격이 짧습니다.")
    if hr_gap is not None and hr_gap >= 90:
        cautions.append("러닝 평균 심박이 안정시 심박 대비 높게 유지됩니다.")

    status = "ok" if not cautions else "caution"
    insight = "회복 상태가 비교적 안정적입니다." if not cautions else " ".join(cautions)

    return {
        "metric": "recovery",
        "latest_value": signals.get("latest_km"),
        "previous_value": signals.get("previous_km"),
        "load_ratio": load_ratio,
        "avg_rest_days": avg_rest_days,
        "avg_session_bpm": avg_bpm,
        "resting_heart_rate": baseline_hr,
        "hr_gap_from_rest": hr_gap,
        "status": status,
        "insight": insight,
    }


async def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    metric = args["metric"]
    weeks = int(args.get("weeks", 4))
    user = ctx["user"]
    guidelines = ctx.get("guidelines", {})

    with sqlite3.connect(ctx["db"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT week_start, session_count, total_km, avg_pace
            FROM v_weekly_summary
            ORDER BY week_start DESC
            LIMIT ?
            """,
            (weeks,),
        ).fetchall()
        series = [dict(row) for row in rows]

        recovery_rows = conn.execute(
            """
            WITH recent_sessions AS (
              SELECT id, session_date, started_at, ended_at, distance_km
              FROM running_sessions
              ORDER BY started_at DESC
              LIMIT ?
            ),
            session_gaps AS (
              SELECT
                id,
                distance_km,
                JULIANDAY(SUBSTR(LAG(started_at) OVER (ORDER BY started_at DESC), 1, 19))
                  - JULIANDAY(SUBSTR(started_at, 1, 19)) AS gap_days
              FROM recent_sessions
            ),
            session_hr AS (
              SELECT
                rs.id,
                ROUND(AVG(hr.bpm), 1) AS avg_bpm
              FROM recent_sessions rs
              LEFT JOIN heart_rate hr ON hr.recorded_at BETWEEN rs.started_at AND rs.ended_at
              GROUP BY rs.id
            )
            SELECT
              ROUND(AVG(sg.gap_days), 2) AS avg_rest_days,
              ROUND(AVG(sh.avg_bpm), 1) AS avg_bpm
            FROM session_gaps sg
            LEFT JOIN session_hr sh ON sh.id = sg.id
            """,
            (weeks * 2,),
        ).fetchone()

    recovery_signals = {
        "avg_rest_days": _safe_float(recovery_rows["avg_rest_days"]) if recovery_rows else None,
        "avg_bpm": _safe_float(recovery_rows["avg_bpm"]) if recovery_rows else None,
        "resting_heart_rate": user.get("resting_heart_rate"),
        "latest_km": _safe_float(series[0]["total_km"]) if series else None,
        "previous_km": _safe_float(series[1]["total_km"]) if len(series) > 1 else None,
    }
    latest_km = recovery_signals["latest_km"]
    previous_km = recovery_signals["previous_km"]
    recovery_signals["load_ratio"] = round(latest_km / previous_km, 2) if latest_km is not None and previous_km not in (None, 0) else None

    if metric == "pace":
        analysis = _build_pace_insight(series, user, guidelines)
    elif metric == "distance":
        analysis = _build_distance_insight(series, user)
    elif metric == "weekly_mileage":
        analysis = _build_weekly_mileage_insight(series, user)
    else:
        analysis = _build_recovery_signal_detail(recovery_signals)

    return {
        "metric": metric,
        "weeks": weeks,
        "series": series,
        "analysis": analysis,
        "signals": recovery_signals if metric == "recovery" else None,
        "user_context": {
            "running_goal": user.get("running_goal"),
            "target_pace_min_km": user.get("target_pace_min_km"),
            "weekly_target_km": user.get("weekly_target_km"),
        },
    }

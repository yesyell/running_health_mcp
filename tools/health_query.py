# tools/health_query.py
import sqlite3
from typing import Any, Dict, List


TOOL_DEF = {
    "name": "health_query",
    "description": (
        "러닝 데이터를 조회합니다.\n"
        "우선: metric/period 기반의 구조화 조회를 사용하세요.\n"
        "대안: sql 필드로 read-only SELECT/CTE만 제한적으로 실행할 수 있습니다.\n"
        "중요: 페이스는 반드시 v_running_pace 뷰(정지구간 제외)를 사용하세요.\n"
        "트렌드(좋아지고 있어?)는 v_weekly_summary의 주간 avg_pace 조회를 권장합니다."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["pace", "weekly_mileage", "distance", "sessions", "heart_rate"],
                "description": "구조화 조회용 지표",
            },
            "period": {
                "type": "string",
                "enum": ["weekly", "monthly", "recent_sessions"],
                "description": "조회 기간 단위",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "구조화 조회 결과 수",
            },
            "sql": {"type": "string", "description": "실행할 SQLite SELECT SQL. 구조화 조회를 쓸 수 없을 때만 사용"},
        },
        "anyOf": [
            {"required": ["sql"]},
            {"required": ["metric", "period"]},
        ],
    },
}


ALLOWED_SQL_SOURCES = [
    "v_running_pace",
    "v_weekly_summary",
    "running_sessions",
    "running_splits",
    "heart_rate",
]


def _is_readonly_sql(sql: str) -> bool:
    s = (sql or "").strip().lower()
    # 단일 statement + SELECT/CTE만 허용
    if ";" in s.strip().rstrip(";"):
        return False
    allowed_starts = ("select", "with")
    if not s.startswith(allowed_starts):
        return False
    blocked = ["insert", "update", "delete", "drop", "alter", "create", "attach", "pragma", "vacuum", "replace"]
    return not any(b in s for b in blocked)


def _uses_allowed_sources(sql: str) -> bool:
    s = (sql or "").lower()
    return any(source in s for source in ALLOWED_SQL_SOURCES)


def _has_limit(sql: str) -> bool:
    return " limit " in f" {sql.strip().lower()} "


def _build_structured_query(metric: str, period: str) -> str:
    if period == "weekly":
        mapping = {
            "pace": """
                SELECT week_start, avg_pace
                FROM v_weekly_summary
                ORDER BY week_start DESC
                LIMIT ?
            """,
            "weekly_mileage": """
                SELECT week_start, total_km
                FROM v_weekly_summary
                ORDER BY week_start DESC
                LIMIT ?
            """,
            "distance": """
                SELECT week_start, total_km
                FROM v_weekly_summary
                ORDER BY week_start DESC
                LIMIT ?
            """,
            "sessions": """
                SELECT week_start, session_count
                FROM v_weekly_summary
                ORDER BY week_start DESC
                LIMIT ?
            """,
            "heart_rate": """
                SELECT
                  DATE(rs.session_date, 'weekday 1', '-7 days') AS week_start,
                  ROUND(AVG(hr.bpm), 1) AS avg_bpm
                FROM running_sessions rs
                JOIN heart_rate hr ON hr.recorded_at BETWEEN rs.started_at AND rs.ended_at
                GROUP BY week_start
                ORDER BY week_start DESC
                LIMIT ?
            """,
        }
    elif period == "monthly":
        mapping = {
            "pace": """
                SELECT
                  STRFTIME('%Y-%m-01', rs.session_date) AS month_start,
                  ROUND(AVG(p.pace_min_per_km), 3) AS avg_pace
                FROM running_sessions rs
                LEFT JOIN v_running_pace p ON p.session_id = rs.id
                GROUP BY month_start
                ORDER BY month_start DESC
                LIMIT ?
            """,
            "weekly_mileage": """
                SELECT
                  STRFTIME('%Y-%m-01', session_date) AS month_start,
                  ROUND(SUM(distance_km), 2) AS total_km
                FROM running_sessions
                GROUP BY month_start
                ORDER BY month_start DESC
                LIMIT ?
            """,
            "distance": """
                SELECT
                  STRFTIME('%Y-%m-01', session_date) AS month_start,
                  ROUND(SUM(distance_km), 2) AS total_km
                FROM running_sessions
                GROUP BY month_start
                ORDER BY month_start DESC
                LIMIT ?
            """,
            "sessions": """
                SELECT
                  STRFTIME('%Y-%m-01', session_date) AS month_start,
                  COUNT(*) AS session_count
                FROM running_sessions
                GROUP BY month_start
                ORDER BY month_start DESC
                LIMIT ?
            """,
            "heart_rate": """
                SELECT
                  STRFTIME('%Y-%m-01', rs.session_date) AS month_start,
                  ROUND(AVG(hr.bpm), 1) AS avg_bpm
                FROM running_sessions rs
                JOIN heart_rate hr ON hr.recorded_at BETWEEN rs.started_at AND rs.ended_at
                GROUP BY month_start
                ORDER BY month_start DESC
                LIMIT ?
            """,
        }
    elif period == "recent_sessions":
        mapping = {
            "pace": """
                SELECT session_date, session_id, ROUND(pace_min_per_km, 3) AS pace_min_per_km
                FROM v_running_pace
                ORDER BY session_date DESC, started_at DESC
                LIMIT ?
            """,
            "weekly_mileage": """
                SELECT session_date, id AS session_id, distance_km
                FROM running_sessions
                ORDER BY started_at DESC
                LIMIT ?
            """,
            "distance": """
                SELECT session_date, id AS session_id, distance_km
                FROM running_sessions
                ORDER BY started_at DESC
                LIMIT ?
            """,
            "sessions": """
                SELECT session_date, id AS session_id, duration_min, distance_km
                FROM running_sessions
                ORDER BY started_at DESC
                LIMIT ?
            """,
            "heart_rate": """
                SELECT
                  rs.session_date,
                  rs.id AS session_id,
                  ROUND(AVG(hr.bpm), 1) AS avg_bpm
                FROM running_sessions rs
                JOIN heart_rate hr ON hr.recorded_at BETWEEN rs.started_at AND rs.ended_at
                GROUP BY rs.id, rs.session_date
                ORDER BY rs.started_at DESC
                LIMIT ?
            """,
        }
    else:
        raise ValueError(f"Unsupported period: {period}")

    if metric not in mapping:
        raise ValueError(f"Unsupported metric for period: {metric}/{period}")
    return mapping[metric]


async def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    db = ctx["db"]
    user = ctx["user"]
    limit = min(max(int(args.get("limit", 8)), 1), 20)
    sql = args.get("sql")
    query_mode = "sql"

    if args.get("metric") and args.get("period"):
        try:
            sql = _build_structured_query(args["metric"], args["period"])
            query_mode = "structured"
        except Exception as e:
            return {"error": f"structured_query_failed: {type(e).__name__}: {e}"}

    if not sql:
        return {"error": "Provide either sql or metric+period."}

    if not _is_readonly_sql(sql):
        return {
            "error": "Only read-only SELECT/CTE SQL is allowed.",
            "hint": "Use SELECT ... FROM v_running_pace / v_weekly_summary ...",
        }
    if not _uses_allowed_sources(sql):
        return {
            "error": "Query must reference an approved source.",
            "allowed_sources": ALLOWED_SQL_SOURCES,
        }
    if query_mode == "sql" and not _has_limit(sql):
        return {
            "error": "SQL mode requires an explicit LIMIT clause.",
            "hint": "Add LIMIT 20 or less.",
        }

    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            if query_mode == "structured":
                rows = conn.execute(sql, (limit,)).fetchall()
            else:
                rows = conn.execute(sql).fetchall()
            data: List[Dict[str, Any]] = [dict(r) for r in rows]
    except Exception as e:
        return {"error": f"SQL execution failed: {type(e).__name__}: {e}"}

    return {
        "data": data,
        "query": {
            "mode": query_mode,
            "metric": args.get("metric"),
            "period": args.get("period"),
            "limit": limit if query_mode == "structured" else None,
        },
        "context": {
            "user_target_pace": user.get("target_pace_min_km"),
            "user_goal": user.get("running_goal"),
            "weekly_target_km": user.get("weekly_target_km"),
            "preferred_distance_km": user.get("preferred_distance_km"),
            "row_count": len(data),
        },
    }

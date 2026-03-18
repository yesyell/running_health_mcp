# tools/health_interpret.py
from typing import Any, Dict, List


TOOL_DEF = {
    "name": "health_interpret",
    "description": (
        "사용자 질의에서 러닝/마라톤 도메인 개념을 파악합니다.\n"
        "- '페이스'는 정지구간 제외 계산(v_running_pace)\n"
        "- '유지시간'은 연속 구간 최댓값(개념에 sql_hint 제공)\n"
        "- '좋아지고 있어?/트렌드/추이'는 4주 이동평균 트렌드 분석을 유도합니다."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "user_query": {"type": "string", "description": "사용자 자연어 질문"},
        },
        "required": ["user_query"],
    },
}


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _infer_period(qn: str) -> Dict[str, Any]:
    period_markers = [
        ("최근 4주", {"type": "relative_weeks", "value": 4}),
        ("4주", {"type": "relative_weeks", "value": 4}),
        ("최근 8주", {"type": "relative_weeks", "value": 8}),
        ("8주", {"type": "relative_weeks", "value": 8}),
        ("이번 주", {"type": "current_week", "value": 1}),
        ("지난주", {"type": "previous_week", "value": 1}),
        ("저번주", {"type": "previous_week", "value": 1}),
        ("이번 달", {"type": "current_month", "value": 1}),
        ("지난달", {"type": "previous_month", "value": 1}),
        ("이번달", {"type": "current_month", "value": 1}),
    ]
    for marker, period in period_markers:
        if marker in qn:
            return period
    return {"type": "unspecified", "value": None}


def _infer_aggregation(qn: str) -> str:
    if any(marker in qn for marker in ["평균", "avg", "average"]):
        return "average"
    if any(marker in qn for marker in ["최대", "가장 긴", "최고"]):
        return "max"
    if any(marker in qn for marker in ["최소", "가장 짧", "최저"]):
        return "min"
    if any(marker in qn for marker in ["합계", "총", "누적"]):
        return "sum"
    return "latest"


def _infer_intent(qn: str, matched: List[Dict[str, Any]]) -> str:
    if any(marker in qn for marker in ["추천", "코스", "어디", "달릴까"]):
        return "recommendation"
    if any(marker in qn for marker in ["좋아지", "나빠지", "트렌드", "추이", "변화", "개선", "악화"]):
        return "trend"
    if any(marker in qn for marker in ["리포트", "요약", "정리"]):
        return "report"
    if any(marker in qn for marker in ["비교", "대비", "차이"]):
        return "comparison"
    if matched:
        return "metric_lookup"
    return "unknown"


def _map_query_metric(concept_keys: List[str]) -> str:
    if "pace" in concept_keys:
        return "pace"
    if "weekly_mileage" in concept_keys:
        return "weekly_mileage"
    if "distance" in concept_keys:
        return "distance"
    if "heart_rate" in concept_keys:
        return "heart_rate"
    return "sessions"


def _map_insight_metric(concept_keys: List[str]) -> str:
    if "recovery" in concept_keys or "heart_rate" in concept_keys:
        return "recovery"
    if "pace" in concept_keys:
        return "pace"
    if "weekly_mileage" in concept_keys:
        return "weekly_mileage"
    if "distance" in concept_keys:
        return "distance"
    return "distance"


def _map_period_to_query_period(period: Dict[str, Any], intent: str) -> str:
    period_type = period.get("type")
    if period_type in {"current_month", "previous_month"}:
        return "monthly"
    if intent == "metric_lookup" and period_type == "unspecified":
        return "recent_sessions"
    return "weekly"


def _map_period_to_weeks(period: Dict[str, Any]) -> int:
    period_type = period.get("type")
    value = period.get("value")
    if period_type == "relative_weeks" and isinstance(value, int):
        return min(max(value, 2), 12)
    if period_type in {"current_week", "previous_week"}:
        return 2
    return 4


def _build_next_actions(intent: str, matched: List[Dict[str, Any]], period: Dict[str, Any]) -> List[Dict[str, Any]]:
    concept_keys = [item["key"] for item in matched]
    if not concept_keys and intent == "report":
        return [
            {
                "tool": "health_report",
                "arguments": {
                    "period": "monthly" if period.get("type") in {"current_month", "previous_month"} else "weekly",
                    "n": _map_period_to_weeks(period),
                },
                "reason": "개념 매칭이 없어도 요약 요청은 리포트로 연결",
            }
        ]
    if not concept_keys and intent == "recommendation":
        return [
            {
                "tool": "running_recommend",
                "arguments": {},
                "reason": "개념 매칭이 없어도 추천 요청은 추천 tool로 연결",
            }
        ]
    if not concept_keys:
        return []

    query_metric = _map_query_metric(concept_keys)
    insight_metric = _map_insight_metric(concept_keys)
    query_period = _map_period_to_query_period(period, intent)
    weeks = _map_period_to_weeks(period)

    actions: List[Dict[str, Any]] = []

    if intent in {"metric_lookup", "comparison", "report", "trend"}:
        actions.append(
            {
                "tool": "health_query",
                "arguments": {
                    "metric": query_metric,
                    "period": query_period,
                    "limit": weeks if query_period == "weekly" else min(weeks, 8),
                },
                "reason": "해석된 지표와 기간에 맞는 구조화 조회",
            }
        )

    if intent in {"trend", "comparison"} or insight_metric == "recovery":
        actions.append(
            {
                "tool": "health_insight",
                "arguments": {
                    "metric": insight_metric,
                    "weeks": weeks,
                },
                "reason": "추세/목표 대비 인사이트 생성",
            }
        )

    if intent == "report":
        actions.append(
            {
                "tool": "health_report",
                "arguments": {
                    "period": "monthly" if query_period == "monthly" else "weekly",
                    "n": weeks,
                },
                "reason": "리포트 형태 요약 생성",
            }
        )

    if intent == "recommendation":
        actions.append(
            {
                "tool": "running_recommend",
                "arguments": {},
                "reason": "추천 시나리오로 연결",
            }
        )

    return actions


async def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    query: str = args["user_query"]
    concepts: Dict[str, Any] = ctx["concepts"]
    guidelines: Dict[str, List[str]] = ctx.get("guidelines", {})

    qn = _normalize(query)

    matched: List[Dict[str, Any]] = []
    for key, concept in concepts.items():
        label = concept.get("label", key)
        aliases = concept.get("aliases") or [label]
        # 간단 substring 매칭 (설계서 수준). 필요하면 토큰화/퍼지매칭으로 확장 가능.
        if any(_normalize(alias) in qn for alias in aliases):
            matched.append(
                {
                    "key": key,
                    "label": label,
                    "db_view": concept.get("db_view"),
                    "db_column": concept.get("db_column"),
                    "sql_hint": concept.get("sql_hint", ""),
                    "calculation": concept.get("calculation", ""),
                    "interpretation": concept.get("interpretation", {}),
                    "edge_cases": concept.get("edge_cases", []),
                    "common_misuse": concept.get("common_misuse", ""),
                    "guidelines": guidelines.get(key, []),
                }
            )

    trend_markers = ["좋아지", "나빠지", "트렌드", "추이", "변화", "개선", "악화", "늘었", "줄었"]
    is_trend_query = any(m in qn for m in trend_markers)
    period = _infer_period(qn)
    aggregation = _infer_aggregation(qn)
    intent = _infer_intent(qn, matched)
    next_actions = _build_next_actions(intent, matched, period)

    return {
        "matched_concepts": matched,
        "is_trend_query": is_trend_query,
        "intent": intent,
        "period": period,
        "aggregation": aggregation,
        "next_actions": next_actions,
        "user_baseline": ctx["user"],
    }

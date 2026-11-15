"""Eigene, versionierte GraphQL-Abfragen für Trackman (Schema per offener Introspection geprüft).

Zeitfelder heißen je Strokes-Art unterschiedlich nullable (`RangeStroke.time` DateTime,
`Stroke.time` DateTime!). Deshalb tragen sie je Art einen eigenen Alias (`rangeTime`,
`strokeTime`), sonst lehnt der Server die Abfrage wegen widersprüchlicher Typen ab.
Es werden nur Felder gewählt, die in allen beteiligten Typen denselben Typ haben.
"""

from __future__ import annotations

QUERY_VERSION = "2026-11-14"

# Aktivitätsarten mit Schlagdaten ohne Lochbezug; COURSE_PLAY kommt über die Scorecards.
RANGE_KINDS = (
    "RANGE_PRACTICE",
    "RANGE_FIND_MY_DISTANCE",
    "SHOT_ANALYSIS",
    "VIRTUAL_RANGE",
    "SESSION",
    "MAP_MY_BAG",
    "SIMULATOR",
)

_BALL = (
    "ballSpeed carry total carrySide totalSide launchAngle launchDirection landingAngle "
    "maxHeight spinAxis curve hangTime"
)
_RANGE_MEASUREMENT = f"{_BALL} ballSpin"
_STROKE_MEASUREMENT = (
    f"{_BALL} spinRate clubSpeed attackAngle clubPath faceAngle faceToPath dynamicLoft "
    "smashFactor spinLoft"
)

_RANGE_TYPES = (
    "RangePracticeActivity",
    "RangeFindMyDistanceActivity",
)
_STROKE_TYPES = (
    "ShotAnalysisSessionActivity",
    "VirtualRangeSessionActivity",
    "SessionActivity",
    "MapMyBagSessionActivity",
    "SimulatorSessionActivity",
)

LIST_ACTIVITIES = """
query Activities($kinds: [ActivityKind!], $from: DateTime, $skip: Int, $take: Int) {
  me { activities(kinds: $kinds, timeFrom: $from, skip: $skip, take: $take, includeHidden: true) {
    totalCount
    items { id time kind }
  } }
}
"""

SESSION_DETAIL = (
    "query Session($id: ID!) { node(id: $id) { id\n"
    + "\n".join(
        f"  ... on {t} {{ time kind strokes {{ club rangeTime: time bayName "
        f"measurement {{ {_RANGE_MEASUREMENT} }} }} }}"
        for t in _RANGE_TYPES
    )
    + "\n"
    + "\n".join(
        f"  ... on {t} {{ time kind strokes {{ club strokeTime: time "
        f"measurement {{ {_STROKE_MEASUREMENT} }} }} }}"
        for t in _STROKE_TYPES
    )
    + "\n} }"
)

_POSITION = "x y z worldLocation { latitude longitude }"

SCORECARDS = f"""
query Scorecards($skip: Int, $take: Int) {{
  me {{ scorecards(skip: $skip, take: $take) {{
    id kind startedAt finishedAt createdAt lastUpdatedAt isCompleted isInHcp
    numberOfHolesPlayed numberOfHolesToPlay par grossScore toPar courseHcp teeName bayKind
    course {{ displayName numbersOfHoles worldLocation {{ latitude longitude }} }}
    tees {{ name gender par rating slope distance }}
    holes {{
      holeNumber par strokeIndex distance grossScore putts greenInRegulation isPlayed
      pinPosition {{ {_POSITION} }}
      shots {{
        shotNumber club total launchLie finalLie shotResult launchTime
        launchPosition {{ {_POSITION} }} finalPosition {{ {_POSITION} }}
        dropPosition {{ {_POSITION} }}
      }}
    }}
  }} }}
}}
"""

PROFILE_CHECK = "query Who { me { profile { id } } }"

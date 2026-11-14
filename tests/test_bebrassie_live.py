"""BeBrassie-Live-Client gegen einen Fake-Transport (kein Netz)."""

import json
from typing import Any

import pytest

from shotlake.connectors.api import AuthError, RateLimitedError, TransientError
from shotlake.connectors.bebrassie_live import BeBrassieClient, slim
from shotlake.connectors.errors import SchemaDriftError
from shotlake.connectors.http import Response

BASE = "https://api.test/v1"
SECRETS = ("geheim@example.org", "pw-GEHEIM")


class FakeApi:
    """Fake für Login, Rundenliste (mit x-total-count) und show-round."""

    def __init__(self, rounds: list[dict[str, Any]], details: dict[str, dict[str, Any]]):
        self.rounds, self.details = rounds, details
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.login_response = Response(200, json.dumps({"success": True, "data": {"token": "T0K"}}))
        self.fail_next: Response | None = None

    def post_json(self, url: str, payload: dict[str, Any], headers: Any = None) -> Response:
        self.calls.append(("POST", url, {}))
        assert payload["email"] and payload["password"] and payload["lang"]
        return self.login_response

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        self.calls.append(("GET", url, dict(headers or {})))
        if self.fail_next:
            resp, self.fail_next = self.fail_next, None
            return resp
        path = url[len(BASE) + 1 :].split("?")[0]
        if path == "PlayedRound":
            query = dict(p.split("=", 1) for p in url.split("?")[1].split("&"))
            skip, limit = int(query["skip"]), int(query["limit"])
            batch = self.rounds[skip : skip + limit]
            return Response(
                200, json.dumps({"data": batch}), {"x-total-count": str(len(self.rounds))}
            )
        if path.startswith("show-round/"):
            rid = path.split("/", 1)[1]
            if rid not in self.details:
                return Response(200, json.dumps({"success": False, "message": "weg"}))
            return Response(200, json.dumps({"success": True, "data": self.details[rid]}))
        return Response(404, "")


def client(api: FakeApi) -> BeBrassieClient:
    return BeBrassieClient(SECRETS[0], SECRETS[1], transport=api, base_url=BASE)


def rounds(n: int) -> list[dict[str, Any]]:
    return [{"_id": f"r{i}", "startedAt": f"2026-07-{i + 1:02d}T10:00:00.000Z"} for i in range(n)]


def test_login_pagination_and_detail():
    api = FakeApi(rounds(120), {"r3": {"_id": "r3", "totalHoles": 9, "course": {"name": "X"}}})
    c = client(api)
    c.login()
    listed = c.list_rounds()
    assert len(listed) == 120 and listed[0]["_id"] == "r0" and listed[-1]["_id"] == "r119"
    pages = [u for m, u, _ in api.calls if "PlayedRound" in u]
    assert len(pages) == 3  # 50 + 50 + 20
    assert "limit=50" in pages[0] and "skip=100" in pages[2] and "deepPopulate=course" in pages[0]
    assert c.show_round("r3") == {"_id": "r3", "totalHoles": 9, "course": {"name": "X"}}
    assert c.show_round("fehlt") is None  # success=false ist kein Abbruch


def test_bearer_token_is_used_only_if_login_returns_one():
    api = FakeApi(rounds(1), {})
    c = client(api)
    c.login()
    c.list_rounds()
    assert api.calls[-1][2]["authorization"] == "Bearer T0K"
    api2 = FakeApi(rounds(1), {})
    api2.login_response = Response(200, json.dumps({"success": True}))
    c2 = client(api2)
    c2.login()
    c2.list_rounds()
    assert "authorization" not in api2.calls[-1][2]


@pytest.mark.parametrize(
    "response",
    [Response(401, ""), Response(403, ""), Response(422, ""),
     Response(200, json.dumps({"success": False}))],
)  # fmt: skip
def test_rejected_login_is_auth_error_without_secrets(response: Response):
    api = FakeApi([], {})
    api.login_response = response
    with pytest.raises(AuthError) as info:
        client(api).login()
    assert not any(secret in str(info.value) for secret in SECRETS)


def test_not_logged_in_and_http_errors():
    api = FakeApi(rounds(1), {})
    with pytest.raises(AuthError, match="Nicht angemeldet"):
        client(api).list_rounds()
    c = client(api)
    c.login()
    for status, error in ((401, AuthError), (429, RateLimitedError), (503, TransientError),
                          (418, SchemaDriftError)):  # fmt: skip
        api.fail_next = Response(status, "", {"retry-after": "5"})
        with pytest.raises(error):
            c.list_rounds()


def test_garbled_responses_are_drift():
    api = FakeApi(rounds(1), {})
    c = client(api)
    c.login()
    api.fail_next = Response(200, "<html>")
    with pytest.raises(SchemaDriftError):
        c.list_rounds()
    api.fail_next = Response(200, json.dumps({"keinData": []}))
    with pytest.raises(SchemaDriftError, match="data"):
        c.list_rounds()
    api.fail_next = Response(200, json.dumps([1, 2]))
    with pytest.raises(SchemaDriftError):
        c.show_round("r1")


def test_client_errors_on_show_round_mean_no_detail_but_auth_errors_still_raise():
    api = FakeApi(rounds(1), {})
    c = client(api)
    c.login()
    api.fail_next = Response(400, "Bad Request")
    assert c.show_round("score-only-runde") is None  # kein Abbruch des Syncs
    api.fail_next = Response(404, "")
    assert c.show_round("x") is None
    for status, error in ((401, AuthError), (429, RateLimitedError), (503, TransientError)):
        api.fail_next = Response(status, "", {"retry-after": "3"})
        with pytest.raises(error):
            c.show_round("x")
    # die Rundenliste bleibt strikt: 400 dort ist ein echter Fehler
    api.fail_next = Response(400, "")
    with pytest.raises(SchemaDriftError):
        c.list_rounds()


def test_empty_list_terminates():
    api = FakeApi([], {})
    c = client(api)
    c.login()
    assert c.list_rounds() == []


def test_slim_removes_geometry_and_user_data_without_touching_the_input():
    data = {
        "_id": "r1",
        "course": {"name": "X", "courseData": {"huge": [1] * 1000}},
        "courseTee": {"name": "Rot", "locations": [1], "teeboxShapes": [2], "ratingMen": 70.1},
        "courseTees": [{"a": 1}],
        "ogTags": {"t": 1},
        "user": {"name": "Klarname"},
        "shots": [{"x": 1}],
    }
    out = slim(data)
    assert "courseData" not in out["course"] and out["course"]["name"] == "X"
    assert out["courseTee"] == {"name": "Rot", "ratingMen": 70.1}
    assert all(k not in out for k in ("courseTees", "ogTags", "user")) and out["shots"] == [
        {"x": 1}
    ]
    assert "courseData" in data["course"] and "user" in data  # Original unverändert
    assert slim({"course": None, "courseTee": "kaputt"}) == {"course": None, "courseTee": "kaputt"}


def test_repr_hides_credentials():
    assert not any(secret in repr(client(FakeApi([], {}))) for secret in SECRETS)

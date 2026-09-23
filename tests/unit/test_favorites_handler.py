"""
Unit tests for the favorites Lambda handler.

All tests inject a mock FavoritesService via the module-level
``_set_service`` helper, so no real AWS or HTTP calls are made.

Coverage
--------
handler()
    - Correct routing for GET, POST, DELETE, OPTIONS
    - 405 for unsupported methods

GET /favorites (_list_favorites)
    - Success: returns 200 with serialised favorites list
    - Empty list returns 200 with empty array
    - user_id defaults to "default" when absent from query params
    - user_id read from query params when present
    - CORS headers present
    - DatabaseError → 500
    - Unexpected error → 500

POST /favorites (_add_favorite)
    - Success: returns 201 with serialised favorite
    - Missing required fields → 400
    - Invalid JSON body → 400
    - Non-numeric lat/lon → 400
    - FavoriteLimitExceededError → 422
    - DuplicateFavoriteError → 409
    - ValidationError → 400
    - DatabaseError → 500
    - user_id taken from body when present
    - user_id taken from query params when not in body

DELETE /favorites/{location_id} (_remove_favorite)
    - Success: returns 204
    - Missing location_id path param → 400
    - DatabaseError → 500
    - user_id defaults to "default"
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    FavoriteLimitExceededError,
    ValidationError,
)
from src.domain.models import FavoriteLocation, Location
import src.api.handlers.favorites_handler as fav_handler
from src.api.handlers.favorites_handler import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_location(loc_id: str = "london-uk") -> Location:
    return Location(
        id=loc_id,
        name="London",
        region="England",
        country="UK",
        latitude=51.5074,
        longitude=-0.1278,
        timezone="Europe/London",
    )


def _make_favorite(
    user_id: str = "user-1",
    loc_id: str = "london-uk",
    current_temperature: float | None = 15.5,
) -> FavoriteLocation:
    return FavoriteLocation(
        user_id=user_id,
        location=_make_location(loc_id),
        added_at=_NOW,
        last_accessed=_NOW,
        current_temperature=current_temperature,
    )


def _mock_service(**overrides) -> MagicMock:
    """Build a MagicMock FavoritesService with async method defaults."""
    svc = MagicMock()
    svc.list_favorites = overrides.get("list_favorites", AsyncMock(return_value=[]))
    svc.add_favorite = overrides.get("add_favorite", AsyncMock(return_value=_make_favorite()))
    svc.remove_favorite = overrides.get("remove_favorite", AsyncMock(return_value=None))
    return svc


def _get_event(
    method: str = "GET",
    query_params: dict | None = None,
    path_params: dict | None = None,
    body: dict | str | None = None,
) -> dict:
    event: dict = {
        "httpMethod": method,
        "queryStringParameters": query_params,
        "pathParameters": path_params,
    }
    if body is None:
        event["body"] = None
    elif isinstance(body, dict):
        event["body"] = json.dumps(body)
    else:
        event["body"] = body
    return event


def _valid_post_body(**overrides) -> dict:
    base = {
        "id": "london-uk",
        "name": "London",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "region": "England",
        "country": "UK",
        "timezone": "Europe/London",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def reset_service():
    """Reset the module-level service before/after each test."""
    fav_handler._set_service(None)
    yield
    fav_handler._set_service(None)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_get_routes_to_list(self):
        svc = _mock_service(list_favorites=AsyncMock(return_value=[]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        assert resp["statusCode"] == 200
        svc.list_favorites.assert_awaited_once()

    def test_post_routes_to_add(self):
        fav = _make_favorite()
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 201
        svc.add_favorite.assert_awaited_once()

    def test_delete_routes_to_remove(self):
        svc = _mock_service(remove_favorite=AsyncMock(return_value=None))
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "london-uk"}), None
        )
        assert resp["statusCode"] == 204
        svc.remove_favorite.assert_awaited_once()

    def test_options_returns_200(self):
        resp = handler(_get_event("OPTIONS"), None)
        assert resp["statusCode"] == 200

    def test_unsupported_method_returns_405(self):
        resp = handler(_get_event("PATCH"), None)
        assert resp["statusCode"] == 405

    def test_put_returns_405(self):
        resp = handler(_get_event("PUT"), None)
        assert resp["statusCode"] == 405


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------


class TestCorsHeaders:
    def test_cors_origin_on_get(self):
        fav_handler._set_service(_mock_service())
        resp = handler(_get_event("GET"), None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_origin_on_post(self):
        fav = _make_favorite()
        fav_handler._set_service(_mock_service(add_favorite=AsyncMock(return_value=fav)))
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_origin_on_delete(self):
        fav_handler._set_service(_mock_service())
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "x"}), None
        )
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_origin_on_405(self):
        resp = handler(_get_event("PATCH"), None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# GET /favorites
# ---------------------------------------------------------------------------


class TestGetFavorites:
    def test_success_returns_200(self):
        fav = _make_favorite()
        svc = _mock_service(list_favorites=AsyncMock(return_value=[fav]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        assert resp["statusCode"] == 200

    def test_success_body_contains_favorites(self):
        fav = _make_favorite()
        svc = _mock_service(list_favorites=AsyncMock(return_value=[fav]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        body = json.loads(resp["body"])
        assert len(body["favorites"]) == 1
        assert body["favorites"][0]["location"]["id"] == "london-uk"

    def test_empty_list_returns_200_empty_array(self):
        svc = _mock_service(list_favorites=AsyncMock(return_value=[]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"favorites": []}

    def test_user_id_defaults_to_default(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        handler(_get_event("GET"), None)
        svc.list_favorites.assert_awaited_once_with("default")

    def test_user_id_from_query_params(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        handler(_get_event("GET", query_params={"user_id": "alice"}), None)
        svc.list_favorites.assert_awaited_once_with("alice")

    def test_serialised_favorite_fields(self):
        fav = _make_favorite(current_temperature=18.0)
        svc = _mock_service(list_favorites=AsyncMock(return_value=[fav]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        item = json.loads(resp["body"])["favorites"][0]
        assert item["user_id"] == "user-1"
        assert item["current_temperature"] == 18.0
        assert "added_at" in item
        assert "last_accessed" in item

    def test_null_temperature_serialised_as_none(self):
        fav = _make_favorite(current_temperature=None)
        svc = _mock_service(list_favorites=AsyncMock(return_value=[fav]))
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        item = json.loads(resp["body"])["favorites"][0]
        assert item["current_temperature"] is None

    def test_database_error_returns_500(self):
        svc = _mock_service(
            list_favorites=AsyncMock(side_effect=DatabaseError("db down"))
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        assert resp["statusCode"] == 500

    def test_unexpected_error_returns_500(self):
        svc = _mock_service(
            list_favorites=AsyncMock(side_effect=RuntimeError("boom"))
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("GET"), None)
        assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# POST /favorites
# ---------------------------------------------------------------------------


class TestPostFavorites:
    def test_success_returns_201(self):
        fav = _make_favorite()
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 201

    def test_success_body_matches_serialised_favorite(self):
        fav = _make_favorite(current_temperature=12.0)
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        body = json.loads(resp["body"])
        assert body["location"]["id"] == "london-uk"
        assert body["current_temperature"] == 12.0

    def test_missing_id_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        body = _valid_post_body()
        del body["id"]
        resp = handler(_get_event("POST", body=body), None)
        assert resp["statusCode"] == 400
        assert "id" in json.loads(resp["body"])["error"]

    def test_missing_name_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        body = _valid_post_body()
        del body["name"]
        resp = handler(_get_event("POST", body=body), None)
        assert resp["statusCode"] == 400

    def test_missing_latitude_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        body = _valid_post_body()
        del body["latitude"]
        resp = handler(_get_event("POST", body=body), None)
        assert resp["statusCode"] == 400

    def test_missing_longitude_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        body = _valid_post_body()
        del body["longitude"]
        resp = handler(_get_event("POST", body=body), None)
        assert resp["statusCode"] == 400

    def test_invalid_json_body_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body="not-json"), None)
        assert resp["statusCode"] == 400

    def test_non_numeric_latitude_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("POST", body=_valid_post_body(latitude="abc")), None
        )
        assert resp["statusCode"] == 400

    def test_non_numeric_longitude_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("POST", body=_valid_post_body(longitude="abc")), None
        )
        assert resp["statusCode"] == 400

    def test_favorite_limit_exceeded_returns_422(self):
        svc = _mock_service(
            add_favorite=AsyncMock(
                side_effect=FavoriteLimitExceededError("limit reached")
            )
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 422
        assert "error" in json.loads(resp["body"])

    def test_duplicate_favorite_returns_409(self):
        svc = _mock_service(
            add_favorite=AsyncMock(
                side_effect=DuplicateFavoriteError("already exists")
            )
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 409
        assert "error" in json.loads(resp["body"])

    def test_validation_error_returns_400(self):
        svc = _mock_service(
            add_favorite=AsyncMock(side_effect=ValidationError("bad input"))
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 400

    def test_database_error_returns_500(self):
        svc = _mock_service(
            add_favorite=AsyncMock(side_effect=DatabaseError("db error"))
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 500

    def test_unexpected_error_returns_500(self):
        svc = _mock_service(
            add_favorite=AsyncMock(side_effect=RuntimeError("boom"))
        )
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=_valid_post_body()), None)
        assert resp["statusCode"] == 500

    def test_user_id_from_body(self):
        fav = _make_favorite(user_id="bob")
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        handler(_get_event("POST", body=_valid_post_body(user_id="bob")), None)
        call_args = svc.add_favorite.await_args
        assert call_args[0][0] == "bob"

    def test_user_id_from_query_params_when_not_in_body(self):
        fav = _make_favorite(user_id="carol")
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        handler(
            _get_event(
                "POST",
                query_params={"user_id": "carol"},
                body=_valid_post_body(),
            ),
            None,
        )
        call_args = svc.add_favorite.await_args
        assert call_args[0][0] == "carol"

    def test_user_id_defaults_to_default(self):
        fav = _make_favorite()
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        handler(_get_event("POST", body=_valid_post_body()), None)
        call_args = svc.add_favorite.await_args
        assert call_args[0][0] == "default"

    def test_location_object_constructed_correctly(self):
        fav = _make_favorite()
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        handler(
            _get_event(
                "POST",
                body=_valid_post_body(
                    id="paris-fr",
                    name="Paris",
                    latitude=48.8566,
                    longitude=2.3522,
                    region="Île-de-France",
                    country="France",
                    timezone="Europe/Paris",
                ),
            ),
            None,
        )
        _, passed_location = svc.add_favorite.await_args[0]
        assert passed_location.id == "paris-fr"
        assert passed_location.name == "Paris"
        assert passed_location.latitude == 48.8566
        assert passed_location.longitude == 2.3522
        assert passed_location.region == "Île-de-France"
        assert passed_location.country == "France"
        assert passed_location.timezone == "Europe/Paris"

    def test_optional_fields_default_when_absent(self):
        """region, country, timezone are optional; absent means empty string / 'UTC'."""
        fav = _make_favorite()
        svc = _mock_service(add_favorite=AsyncMock(return_value=fav))
        fav_handler._set_service(svc)
        handler(
            _get_event("POST", body={"id": "x", "name": "X", "latitude": 0, "longitude": 0}),
            None,
        )
        _, passed_location = svc.add_favorite.await_args[0]
        assert passed_location.region == ""
        assert passed_location.country == ""
        assert passed_location.timezone == "UTC"

    def test_null_body_treated_as_empty(self):
        """A null/missing body should yield a 400 about missing fields, not a crash."""
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(_get_event("POST", body=None), None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# DELETE /favorites/{location_id}
# ---------------------------------------------------------------------------


class TestDeleteFavorite:
    def test_success_returns_204(self):
        svc = _mock_service(remove_favorite=AsyncMock(return_value=None))
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "london-uk"}), None
        )
        assert resp["statusCode"] == 204

    def test_success_body_is_empty(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "london-uk"}), None
        )
        assert resp["body"] == ""

    def test_missing_location_id_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(_get_event("DELETE", path_params={}), None)
        assert resp["statusCode"] == 400

    def test_null_path_params_returns_400(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        resp = handler(_get_event("DELETE", path_params=None), None)
        assert resp["statusCode"] == 400

    def test_location_id_passed_to_service(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        handler(
            _get_event("DELETE", path_params={"location_id": "paris-fr"}), None
        )
        svc.remove_favorite.assert_awaited_once_with("default", "paris-fr")

    def test_user_id_from_query_params(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        handler(
            _get_event(
                "DELETE",
                query_params={"user_id": "dave"},
                path_params={"location_id": "paris-fr"},
            ),
            None,
        )
        svc.remove_favorite.assert_awaited_once_with("dave", "paris-fr")

    def test_user_id_defaults_to_default(self):
        svc = _mock_service()
        fav_handler._set_service(svc)
        handler(
            _get_event("DELETE", path_params={"location_id": "x"}), None
        )
        called_user_id = svc.remove_favorite.await_args[0][0]
        assert called_user_id == "default"

    def test_database_error_returns_500(self):
        svc = _mock_service(
            remove_favorite=AsyncMock(side_effect=DatabaseError("db failure"))
        )
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "london-uk"}), None
        )
        assert resp["statusCode"] == 500

    def test_unexpected_error_returns_500(self):
        svc = _mock_service(
            remove_favorite=AsyncMock(side_effect=RuntimeError("crash"))
        )
        fav_handler._set_service(svc)
        resp = handler(
            _get_event("DELETE", path_params={"location_id": "london-uk"}), None
        )
        assert resp["statusCode"] == 500

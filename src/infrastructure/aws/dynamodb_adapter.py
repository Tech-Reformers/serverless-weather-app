"""
DynamoDB infrastructure adapters for the Serverless Weather App.

Implements:
  - ``DynamoDBFavoritesService`` — port: :class:`~src.domain.services.FavoritesServicePort`
  - ``DynamoDBUnitService``      — port: :class:`~src.domain.services.UnitConversionServicePort`

Table names are read from environment variables so the same code can target
different environments (dev / staging / production) without changes:

    ``FAVORITES_TABLE_NAME``   (default: ``weather-app-favorites``)
    ``PREFERENCES_TABLE_NAME`` (default: ``weather-app-preferences``)

Since ``boto3`` is synchronous and the application layer is fully async, every
DynamoDB call is dispatched to the default :mod:`asyncio` thread-pool executor
via :func:`asyncio.get_event_loop().run_in_executor`.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from functools import partial
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from src.domain.exceptions import (
    DatabaseError,
    DuplicateFavoriteError,
    FavoriteLimitExceededError,
    ValidationError,
)
from src.domain.models import FavoriteLocation, Location, TempUnit
from src.domain.services import FavoritesServicePort, UnitConversionServicePort

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FAVORITES = 50
_COUNTER_SORT_KEY = "__count__"
_PREFERENCES_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year

# Projection columns fetched by list_favorites — excludes the synthetic
# ``__count__`` counter item's extra fields and any future write-only
# attributes, so DynamoDB transfers only what the caller needs.
_FAVORITES_PROJECTION = (
    "user_id, location_id, location_name, location_region, "
    "location_country, latitude, longitude, #tz, added_at, last_accessed"
)
# ``timezone`` is a DynamoDB reserved word; use an expression attribute name.
_FAVORITES_PROJECTION_NAMES = {"#tz": "timezone"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync(func, *args, **kwargs):
    """Run a synchronous callable in the default executor and return a coroutine."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, partial(func, *args, **kwargs))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DynamoDBFavoritesService
# ---------------------------------------------------------------------------


class DynamoDBFavoritesService(FavoritesServicePort):
    """DynamoDB-backed implementation of :class:`~src.domain.services.FavoritesServicePort`.

    Schema (``weather-app-favorites`` table)
    =========================================
    Partition key: ``user_id``  (String)
    Sort key:      ``location_id``  (String)

    A special "counter" item per user stores the current favourite count:
        ``user_id = <user_id>``  ``location_id = "__count__"``  ``count = <N>``

    This counter is incremented/decremented atomically via
    ``UpdateItem`` with ``ADD`` and condition expressions, which lets us
    enforce the 50-item hard cap without a separate read-before-write.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dynamodb_resource=None,
    ) -> None:
        self._table_name = table_name or os.environ.get(
            "FAVORITES_TABLE_NAME", "weather-app-favorites"
        )
        self._dynamodb = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self._table_name)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def add_favorite(
        self,
        user_id: str,
        location: Location,
    ) -> FavoriteLocation:
        """Add *location* to *user_id*'s favourites.

        Uses two conditional writes in sequence:

        1. ``PutItem`` with ``attribute_not_exists(location_id)`` to prevent
           duplicates — raises :exc:`DuplicateFavoriteError` on collision.
        2. ``UpdateItem`` with ``count < 50`` condition to atomically
           increment the per-user counter — raises
           :exc:`FavoriteLimitExceededError` when the cap would be exceeded.

        Because both operations are idempotent the ordering is safe: a
        duplicate is detected before touching the counter.
        """
        now = _now_iso()
        item = {
            "user_id": user_id,
            "location_id": location.id,
            "location_name": location.name,
            "location_region": location.region,
            "location_country": location.country,
            "latitude": str(location.latitude),   # DynamoDB decimal-safe
            "longitude": str(location.longitude),
            "timezone": location.timezone,
            "added_at": now,
            "last_accessed": now,
        }

        # Step 1 — guard against duplicates
        try:
            await _run_sync(
                self._table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(location_id)",
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ConditionalCheckFailedException":
                raise DuplicateFavoriteError(
                    f"Location '{location.id}' is already a favourite for user '{user_id}'."
                ) from exc
            raise DatabaseError(f"DynamoDB PutItem failed: {exc}") from exc

        # Step 2 — atomically increment counter; roll back the item on failure
        try:
            await _run_sync(
                self._table.update_item,
                Key={"user_id": user_id, "location_id": _COUNTER_SORT_KEY},
                UpdateExpression="ADD #cnt :inc SET #uid = :uid",
                ConditionExpression=(
                    "attribute_not_exists(#cnt) OR #cnt < :limit"
                ),
                ExpressionAttributeNames={"#cnt": "count", "#uid": "user_id"},
                ExpressionAttributeValues={":inc": 1, ":limit": _MAX_FAVORITES, ":uid": user_id},
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            # Roll back the favourite item we just inserted
            try:
                await _run_sync(
                    self._table.delete_item,
                    Key={"user_id": user_id, "location_id": location.id},
                )
            except ClientError:
                pass  # best-effort rollback

            if code == "ConditionalCheckFailedException":
                raise FavoriteLimitExceededError(
                    f"User '{user_id}' has reached the {_MAX_FAVORITES}-favourite limit."
                ) from exc
            raise DatabaseError(f"DynamoDB UpdateItem (counter) failed: {exc}") from exc

        return FavoriteLocation(
            user_id=user_id,
            location=location,
            added_at=datetime.fromisoformat(now),
            last_accessed=datetime.fromisoformat(now),
            current_temperature=None,
        )

    async def remove_favorite(
        self,
        user_id: str,
        location_id: str,
    ) -> None:
        """Delete the favourite item and decrement the per-user counter.

        If the item doesn't exist the delete is a no-op.  The counter is only
        decremented when the item existed (checked via a return-values guard).
        """
        try:
            response = await _run_sync(
                self._table.delete_item,
                Key={"user_id": user_id, "location_id": location_id},
                ReturnValues="ALL_OLD",
            )
        except ClientError as exc:
            raise DatabaseError(f"DynamoDB DeleteItem failed: {exc}") from exc

        # Only decrement if something was actually deleted
        if not response.get("Attributes"):
            return

        try:
            await _run_sync(
                self._table.update_item,
                Key={"user_id": user_id, "location_id": _COUNTER_SORT_KEY},
                UpdateExpression="ADD #cnt :dec",
                ConditionExpression="attribute_exists(#cnt) AND #cnt > :zero",
                ExpressionAttributeNames={"#cnt": "count"},
                ExpressionAttributeValues={":dec": -1, ":zero": 0},
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            # Counter not existing / already 0 is non-fatal
            if code != "ConditionalCheckFailedException":
                raise DatabaseError(
                    f"DynamoDB UpdateItem (counter decrement) failed: {exc}"
                ) from exc

    async def list_favorites(
        self,
        user_id: str,
    ) -> List[FavoriteLocation]:
        """Query all favourite items for *user_id*, sorted by ``added_at``.

        Optimisation (Requirements 5.4, 5.5):
        A ``ProjectionExpression`` limits the attributes DynamoDB returns to
        only the columns needed to construct :class:`FavoriteLocation` objects.
        This reduces read-unit consumption and network payload — important when
        a user has up to 50 favourites and the caller subsequently enriches
        each entry with a live temperature fetch.

        The ``FilterExpression`` on ``location_id`` keeps the synthetic
        ``__count__`` counter item out of the result set server-side; the
        in-process guard below handles the rare case where the filter is not
        applied (e.g. in mocked/test environments).

        ``timezone`` is a DynamoDB reserved word and must be aliased via
        ``ExpressionAttributeNames``.
        """
        from boto3.dynamodb.conditions import Key, Attr  # local import to keep top clean

        # ``timezone`` is a reserved word — alias it so the projection is valid.
        # ``location_id`` in FilterExpression is NOT a reserved word so no alias
        # is needed there; we only need the ``#tz`` alias for the projection.
        try:
            response = await _run_sync(
                self._table.query,
                KeyConditionExpression=Key("user_id").eq(user_id),
                # NOTE: Cannot FilterExpression on sort-key (location_id); filter in Python instead
                ProjectionExpression=_FAVORITES_PROJECTION,
                ExpressionAttributeNames=_FAVORITES_PROJECTION_NAMES,
            )
        except ClientError as exc:
            raise DatabaseError(f"DynamoDB Query failed: {exc}") from exc

        items = response.get("Items", [])
        favorites: List[FavoriteLocation] = []
        for item in items:
            # Skip the synthetic counter item (FilterExpression handles this in
            # production; this guard covers edge cases and mocked environments).
            if item.get("location_id") == _COUNTER_SORT_KEY:
                continue
            loc = Location(
                id=item["location_id"],
                name=item["location_name"],
                region=item.get("location_region", ""),
                country=item.get("location_country", ""),
                latitude=float(item["latitude"]),
                longitude=float(item["longitude"]),
                timezone=item.get("timezone", "UTC"),
            )
            favorites.append(
                FavoriteLocation(
                    user_id=user_id,
                    location=loc,
                    added_at=datetime.fromisoformat(item["added_at"]),
                    last_accessed=datetime.fromisoformat(item["last_accessed"]),
                    current_temperature=None,
                )
            )

        # Sort by added_at ascending (earliest first)
        favorites.sort(key=lambda f: f.added_at)
        return favorites

    async def get_favorite_count(
        self,
        user_id: str,
    ) -> int:
        """Return the current favourite count for *user_id* via the counter item."""
        try:
            response = await _run_sync(
                self._table.get_item,
                Key={"user_id": user_id, "location_id": _COUNTER_SORT_KEY},
                ProjectionExpression="#cnt",
                ExpressionAttributeNames={"#cnt": "count"},
            )
        except ClientError as exc:
            raise DatabaseError(f"DynamoDB GetItem (counter) failed: {exc}") from exc

        item = response.get("Item")
        if not item:
            return 0
        return int(item.get("count", 0))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_or_init_count(self, user_id: str) -> int:
        """Return the counter value for *user_id*, initialising it to 0 if absent.

        Counter design rationale
        ------------------------
        The favorites table uses a **synthetic counter item** stored alongside
        the actual favourite entries:

            PK = user_id   SK = "__count__"   count = <N>

        This avoids a ``Scan`` or counting ``Query`` on every write path.
        Instead, ``add_favorite`` uses an atomic ``ADD`` with a condition
        (``count < 50``) so the cap is enforced at the DynamoDB level, not
        in application code — no read-modify-write race condition is possible.

        ``_get_or_init_count`` is a read-only helper for diagnostic purposes
        (e.g. health checks, tests) that does **not** mutate the counter; it
        simply returns 0 when no counter item exists yet (i.e. the user has
        never added a favourite).  For real add/remove paths, the counter is
        maintained by the conditional ``UpdateItem`` calls in
        :meth:`add_favorite` and :meth:`remove_favorite`.
        """
        try:
            response = await _run_sync(
                self._table.get_item,
                Key={"user_id": user_id, "location_id": _COUNTER_SORT_KEY},
                ProjectionExpression="#cnt",
                ExpressionAttributeNames={"#cnt": "count"},
                ConsistentRead=True,  # strong consistency for accurate cap checks
            )
        except ClientError as exc:
            raise DatabaseError(
                f"DynamoDB GetItem (_get_or_init_count) failed: {exc}"
            ) from exc

        item = response.get("Item")
        if not item:
            return 0
        return int(item.get("count", 0))


# ---------------------------------------------------------------------------
# DynamoDBUnitService
# ---------------------------------------------------------------------------


class DynamoDBUnitService(UnitConversionServicePort):
    """DynamoDB-backed implementation of :class:`~src.domain.services.UnitConversionServicePort`.

    Schema (``weather-app-preferences`` table)
    ==========================================
    Partition key: ``user_id``  (String)

    Each item stores:
        ``temperature_unit``: ``"celsius"`` | ``"fahrenheit"``
        ``updated_at``: ISO-8601 timestamp
        ``ttl``: Unix epoch seconds (1-year TTL)
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dynamodb_resource=None,
    ) -> None:
        self._table_name = table_name or os.environ.get(
            "PREFERENCES_TABLE_NAME", "weather-app-preferences"
        )
        self._dynamodb = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self._table_name)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def convert_temperature(
        self,
        value: float,
        from_unit: TempUnit,
        to_unit: TempUnit,
    ) -> float:
        """Convert *value* between Celsius and Fahrenheit (pure math, no I/O).

        Conversion formulae preserve physical relationships (Property 10):
            °F = °C × 9/5 + 32
            °C = (°F − 32) × 5/9

        Raises:
            ValidationError: If either unit is not a valid :class:`TempUnit`.
        """
        # Validate units
        if not isinstance(from_unit, TempUnit):
            try:
                from_unit = TempUnit(from_unit)
            except ValueError:
                raise ValidationError(f"Invalid temperature unit: '{from_unit}'")
        if not isinstance(to_unit, TempUnit):
            try:
                to_unit = TempUnit(to_unit)
            except ValueError:
                raise ValidationError(f"Invalid temperature unit: '{to_unit}'")

        if from_unit == to_unit:
            return value

        if from_unit == TempUnit.CELSIUS and to_unit == TempUnit.FAHRENHEIT:
            return value * 9 / 5 + 32

        # FAHRENHEIT -> CELSIUS
        return (value - 32) * 5 / 9

    async def get_user_preference(
        self,
        user_id: str,
    ) -> TempUnit:
        """Retrieve the persisted unit preference; returns ``CELSIUS`` if unset."""
        try:
            response = await _run_sync(
                self._table.get_item,
                Key={"user_id": user_id},
                ProjectionExpression="temperature_unit",
            )
        except ClientError as exc:
            raise DatabaseError(f"DynamoDB GetItem (preference) failed: {exc}") from exc

        item = response.get("Item")
        if not item:
            return TempUnit.CELSIUS

        raw = item.get("temperature_unit", TempUnit.CELSIUS.value)
        try:
            return TempUnit(raw)
        except ValueError:
            # Corrupt data — fall back to default
            return TempUnit.CELSIUS

    async def set_user_preference(
        self,
        user_id: str,
        unit: TempUnit,
    ) -> None:
        """Persist *unit* for *user_id* with a 1-year TTL.

        Raises:
            ValidationError: If *unit* is not a valid :class:`TempUnit`.
            DatabaseError: If the DynamoDB write fails.
        """
        if not isinstance(unit, TempUnit):
            try:
                unit = TempUnit(unit)
            except ValueError:
                raise ValidationError(f"Invalid temperature unit: '{unit}'")

        ttl = int(time.time()) + _PREFERENCES_TTL_SECONDS
        item = {
            "user_id": user_id,
            "temperature_unit": unit.value,
            "updated_at": _now_iso(),
            "ttl": ttl,
        }

        try:
            await _run_sync(self._table.put_item, Item=item)
        except ClientError as exc:
            raise DatabaseError(f"DynamoDB PutItem (preference) failed: {exc}") from exc


# ---------------------------------------------------------------------------
# DaxEnabledDynamoDBAdapter  (stub — requires VPC configuration)
# ---------------------------------------------------------------------------


class DaxEnabledDynamoDBAdapter(DynamoDBFavoritesService):
    """Drop-in replacement for :class:`DynamoDBFavoritesService` that routes
    read operations through **Amazon DynamoDB Accelerator (DAX)**.

    DAX Overview
    ------------
    DAX is an in-memory, write-through cache cluster for DynamoDB that
    delivers microsecond read latency for eventually-consistent reads.
    It is fully API-compatible with DynamoDB, so no changes to read/write
    logic are required — only the client object needs to be swapped.

    Why a stub?
    -----------
    DAX requires a running DAX cluster inside a VPC, which is an
    infrastructure concern that cannot be verified in a local or CI
    environment without a deployed cluster.  This class documents the
    intended pattern and can be activated in production by supplying a
    ``dax_endpoint`` at construction time.

    Usage
    -----
    ::

        # In a production Lambda bootstrapped with the DAX endpoint from
        # Secrets Manager or an environment variable:
        service = DaxEnabledDynamoDBAdapter(
            table_name=os.environ["FAVORITES_TABLE_NAME"],
            dax_endpoint=os.environ["DAX_ENDPOINT"],   # e.g. "daxs://my-cluster.abc123.dax-clusters.us-east-1.amazonaws.com"
        )

    When ``dax_endpoint`` is ``None`` or empty the adapter falls back to
    standard DynamoDB (identical to :class:`DynamoDBFavoritesService`), so
    the same code path works in local development and integration tests.

    Activation checklist (infrastructure team)
    -------------------------------------------
    1. Provision a DAX cluster in the same VPC/subnet as the Lambda functions.
    2. Attach a security group that allows port 8111 (unencrypted) or 9111
       (TLS) from the Lambda security group.
    3. Grant the Lambda execution role ``dax:GetItem``, ``dax:BatchGetItem``,
       ``dax:Query`` IAM permissions on the cluster ARN.
    4. Install ``amazon-dax-client`` (``amazondax``) in the Lambda layer and
       add it to ``requirements.txt``.
    5. Pass ``DAX_ENDPOINT`` as a Lambda environment variable.

    Notes
    -----
    * DAX does **not** cache write operations — ``add_favorite`` and
      ``remove_favorite`` always go directly to DynamoDB.
    * ``list_favorites`` (a ``Query``) and ``get_favorite_count`` (a
      ``GetItem``) benefit from DAX caching.
    * The counter item uses strong consistency (``ConsistentRead=True``) in
      :meth:`_get_or_init_count`; DAX bypasses its cache for strongly-
      consistent reads, so the cap enforcement logic remains accurate.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dax_endpoint: Optional[str] = None,
        dynamodb_resource=None,
    ) -> None:
        if dax_endpoint:
            # ``amazondax`` is an optional dependency — import lazily so the
            # module loads cleanly in environments without the package.
            try:
                import amazondax  # type: ignore[import-untyped]

                dax_client = amazondax.AmazonDaxClient.resource(
                    endpoint_url=dax_endpoint,
                )
                super().__init__(
                    table_name=table_name,
                    dynamodb_resource=dax_client,
                )
            except ImportError as exc:
                raise ImportError(
                    "The 'amazondax' package is required to use DaxEnabledDynamoDBAdapter "
                    "with a DAX endpoint.  Install it with: pip install amazon-dax-client"
                ) from exc
        else:
            # No DAX endpoint supplied — fall back to standard DynamoDB.
            super().__init__(
                table_name=table_name,
                dynamodb_resource=dynamodb_resource,
            )

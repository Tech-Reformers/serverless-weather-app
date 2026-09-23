"""
WeatherAppStack — AWS CDK Python stack for the Serverless Weather App.

Provisions:
  - DynamoDB tables (favorites, cache, preferences)
  - Lambda functions for each service domain (Python 3.12, 512 MB)
  - API Gateway REST API with Lambda proxy integration
  - Secrets Manager secret for external weather-provider API keys
  - CloudWatch log groups (14-day retention) and metric alarms
  - IAM roles following least-privilege principle

Design reference: .kiro/specs/weather-app/design.md
Requirements reference: .kiro/specs/weather-app/requirements.md
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from constructs import Construct


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lambda runtime and sizing — design.md §AWS Service Mappings
LAMBDA_RUNTIME = _lambda.Runtime.PYTHON_3_12
LAMBDA_MEMORY_MB = 512
LAMBDA_TIMEOUT_EXTERNAL = Duration.seconds(30)  # outer Lambda max timeout
LAMBDA_TIMEOUT_DEFAULT = Duration.seconds(10)

# DynamoDB TTL attribute name (used across cache and preferences tables)
TTL_ATTRIBUTE = "ttl"

# CloudWatch log retention
LOG_RETENTION = logs.RetentionDays.TWO_WEEKS

# API Gateway cache TTL — design.md §Multi-Layer Caching Approach
APIGW_CACHE_TTL = Duration.seconds(300)  # 5 minutes

# Alarm thresholds
ALARM_ERROR_RATE_THRESHOLD = 5          # invocations
ALARM_LATENCY_THRESHOLD_MS = 5_000      # 5 seconds (p99)


class WeatherAppStack(Stack):
    """
    Top-level CDK stack for the Serverless Weather App.

    Resources are created in this order so CDK can resolve dependencies
    cleanly without circular references:
      1. DynamoDB tables
      2. Secrets Manager secret
      3. IAM roles
      4. Lambda functions
      5. API Gateway
      6. CloudWatch log groups & alarms
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # 1. DynamoDB Tables                                                   #
        # ------------------------------------------------------------------ #
        self._create_dynamodb_tables()

        # ------------------------------------------------------------------ #
        # 2. Secrets Manager                                                   #
        # ------------------------------------------------------------------ #
        self._create_secrets()

        # ------------------------------------------------------------------ #
        # 3. IAM roles (one per Lambda — least privilege)                     #
        # ------------------------------------------------------------------ #
        self._create_iam_roles()

        # ------------------------------------------------------------------ #
        # 4. Lambda Functions                                                  #
        # ------------------------------------------------------------------ #
        self._create_lambda_functions()

        # ------------------------------------------------------------------ #
        # 5. API Gateway REST API                                              #
        # ------------------------------------------------------------------ #
        self._create_api_gateway()

        # ------------------------------------------------------------------ #
        # 6. CloudWatch Log Groups & Alarms                                    #
        # ------------------------------------------------------------------ #
        self._create_cloudwatch_resources()

        # ------------------------------------------------------------------ #
        # 7. CloudWatch Dashboard                                              #
        # ------------------------------------------------------------------ #
        self._create_dashboard()

        # ------------------------------------------------------------------ #
        # 8. Stack outputs                                                     #
        # ------------------------------------------------------------------ #
        self._create_outputs()

    # ======================================================================= #
    # DynamoDB Tables                                                          #
    # ======================================================================= #

    def _create_dynamodb_tables(self) -> None:
        """
        Create the three DynamoDB tables described in design.md §Data Models.

        - weather-app-favorites  (PK: user_id, SK: location_id)
        - weather-app-cache      (PK: cache_key, SK: cache_type) + TTL
        - weather-app-preferences (PK: user_id) + TTL
        """

        # -- Favorites table ------------------------------------------------
        self.favorites_table = dynamodb.Table(
            self,
            "FavoritesTable",
            table_name="weather-app-favorites",
            partition_key=dynamodb.Attribute(
                name="user_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="location_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # GSI: look up all users that favorited a specific location_id
        self.favorites_table.add_global_secondary_index(
            index_name="location_id-index",
            partition_key=dynamodb.Attribute(
                name="location_id",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # -- Weather cache table --------------------------------------------
        self.cache_table = dynamodb.Table(
            self,
            "CacheTable",
            table_name="weather-app-cache",
            partition_key=dynamodb.Attribute(
                name="cache_key",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="cache_type",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=TTL_ATTRIBUTE,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,  # cache is ephemeral
        )

        # GSI: look up cache entries by timestamp (for cache invalidation)
        self.cache_table.add_global_secondary_index(
            index_name="timestamp-index",
            partition_key=dynamodb.Attribute(
                name="timestamp",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # -- User preferences table -----------------------------------------
        self.preferences_table = dynamodb.Table(
            self,
            "PreferencesTable",
            table_name="weather-app-preferences",
            partition_key=dynamodb.Attribute(
                name="user_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=TTL_ATTRIBUTE,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ======================================================================= #
    # Secrets Manager                                                          #
    # ======================================================================= #

    def _create_secrets(self) -> None:
        """
        Create a Secrets Manager secret for external weather-provider API keys.
        The secret stores a JSON object with provider key slots so multiple
        providers can be supported without requiring a secret rotation.

        Automatic rotation is enabled every 90 days (design.md §Property 27).
        Rotation requires a rotation Lambda — the placeholder ARN is overridden
        in a real deployment via CDK context or environment variable.
        """
        self.weather_api_secret = secretsmanager.Secret(
            self,
            "WeatherApiSecret",
            secret_name="weather-app/external-api-keys",
            description=(
                "API keys for external weather data providers "
                "(OpenWeather, WeatherAPI, geocoding services)"
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"openweather_api_key": "", "weatherapi_key": "", "geocoding_api_key": ""}',
                generate_string_key="__unused__",  # template drives the shape
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

    # ======================================================================= #
    # IAM Roles                                                                #
    # ======================================================================= #

    def _create_iam_roles(self) -> None:
        """
        One execution role per Lambda function so each service has the
        minimum required permissions (design.md §AWS Security Best Practices).
        """
        # Shared trust policy for all Lambda roles
        lambda_principal = iam.ServicePrincipal("lambda.amazonaws.com")

        # Base managed policy every Lambda needs (CloudWatch Logs + X-Ray)
        base_policies = [
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            ),
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AWSXRayDaemonWriteAccess"
            ),
        ]

        # -- Location Lambda role -------------------------------------------
        self.location_lambda_role = iam.Role(
            self,
            "LocationLambdaRole",
            role_name="weather-app-location-lambda-role",
            assumed_by=lambda_principal,
            managed_policies=base_policies,
        )
        # Location service only needs to read the API secret
        self.weather_api_secret.grant_read(self.location_lambda_role)

        # -- Weather Lambda role --------------------------------------------
        self.weather_lambda_role = iam.Role(
            self,
            "WeatherLambdaRole",
            role_name="weather-app-weather-lambda-role",
            assumed_by=lambda_principal,
            managed_policies=base_policies,
        )
        self.weather_api_secret.grant_read(self.weather_lambda_role)
        self.cache_table.grant_read_write_data(self.weather_lambda_role)

        # -- Forecast Lambda role ------------------------------------------
        self.forecast_lambda_role = iam.Role(
            self,
            "ForecastLambdaRole",
            role_name="weather-app-forecast-lambda-role",
            assumed_by=lambda_principal,
            managed_policies=base_policies,
        )
        self.weather_api_secret.grant_read(self.forecast_lambda_role)
        self.cache_table.grant_read_write_data(self.forecast_lambda_role)

        # -- Favorites Lambda role -----------------------------------------
        self.favorites_lambda_role = iam.Role(
            self,
            "FavoritesLambdaRole",
            role_name="weather-app-favorites-lambda-role",
            assumed_by=lambda_principal,
            managed_policies=base_policies,
        )
        self.favorites_table.grant_read_write_data(self.favorites_lambda_role)
        # Favorites list also fetches current temperatures (needs weather cache read)
        self.cache_table.grant_read_data(self.favorites_lambda_role)

        # -- Preferences (Unit) Lambda role --------------------------------
        self.unit_lambda_role = iam.Role(
            self,
            "UnitLambdaRole",
            role_name="weather-app-unit-lambda-role",
            assumed_by=lambda_principal,
            managed_policies=base_policies,
        )
        self.preferences_table.grant_read_write_data(self.unit_lambda_role)

    # ======================================================================= #
    # Lambda Functions                                                         #
    # ======================================================================= #

    def _create_lambda_functions(self) -> None:
        """
        Create one Lambda function per service domain.
        All functions use Python 3.12 with 512 MB memory (design.md §AWS Service Mappings).
        Common environment variables are injected for every function.
        """
        common_env = {
            "FAVORITES_TABLE_NAME": self.favorites_table.table_name,
            "CACHE_TABLE_NAME": self.cache_table.table_name,
            "PREFERENCES_TABLE_NAME": self.preferences_table.table_name,
            "WEATHER_API_SECRET_ARN": self.weather_api_secret.secret_arn,
            "POWERTOOLS_SERVICE_NAME": "weather-app",
            "LOG_LEVEL": "INFO",
        }

        # Shared Lambda code directory — src/ will be built & deployed here.
        # The inline placeholder code lets CDK synthesise without real source.
        placeholder_code = _lambda.Code.from_inline(
            "def handler(event, context):\n    return {'statusCode': 200, 'body': '{}'}\n"
        )

        # -- Location Lambda -----------------------------------------------
        self.location_lambda = _lambda.Function(
            self,
            "LocationLambda",
            function_name="weather-app-location",
            runtime=LAMBDA_RUNTIME,
            handler="src.api.handlers.location_handler.handler",
            code=placeholder_code,
            memory_size=LAMBDA_MEMORY_MB,
            timeout=LAMBDA_TIMEOUT_DEFAULT,
            role=self.location_lambda_role,
            environment={
                **common_env,
                "EXTERNAL_CALL_TIMEOUT_SECONDS": "10",
                "SEARCH_RESULT_LIMIT": "10",
                "DEVICE_LOCATION_TIMEOUT_SECONDS": "30",
                "NEARBY_LOCATION_RADIUS_KM": "50",
            },
            tracing=_lambda.Tracing.ACTIVE,
            description="Handles location search and device location resolution",
        )

        # -- Weather (Current Conditions) Lambda ---------------------------
        self.weather_lambda = _lambda.Function(
            self,
            "WeatherLambda",
            function_name="weather-app-weather",
            runtime=LAMBDA_RUNTIME,
            handler="src.api.handlers.weather_handler.handler",
            code=placeholder_code,
            memory_size=LAMBDA_MEMORY_MB,
            timeout=LAMBDA_TIMEOUT_DEFAULT,
            role=self.weather_lambda_role,
            environment={
                **common_env,
                "EXTERNAL_CALL_TIMEOUT_SECONDS": "10",
                "CACHE_TTL_CURRENT_MINUTES": "15",
                "REFRESH_TIMEOUT_SECONDS": "5",
            },
            tracing=_lambda.Tracing.ACTIVE,
            description="Retrieves and caches current weather conditions",
        )

        # -- Forecast Lambda -----------------------------------------------
        self.forecast_lambda = _lambda.Function(
            self,
            "ForecastLambda",
            function_name="weather-app-forecast",
            runtime=LAMBDA_RUNTIME,
            handler="src.api.handlers.forecast_handler.handler",
            code=placeholder_code,
            memory_size=LAMBDA_MEMORY_MB,
            timeout=LAMBDA_TIMEOUT_DEFAULT,
            role=self.forecast_lambda_role,
            environment={
                **common_env,
                "EXTERNAL_CALL_TIMEOUT_SECONDS": "10",
                "CACHE_TTL_FORECAST_MINUTES": "60",
                "HOURLY_FORECAST_HOURS": "24",
                "DAILY_FORECAST_DAYS": "7",
            },
            tracing=_lambda.Tracing.ACTIVE,
            description="Retrieves hourly and daily weather forecasts",
        )

        # -- Favorites Lambda ----------------------------------------------
        self.favorites_lambda = _lambda.Function(
            self,
            "FavoritesLambda",
            function_name="weather-app-favorites",
            runtime=LAMBDA_RUNTIME,
            handler="src.api.handlers.favorites_handler.handler",
            code=placeholder_code,
            memory_size=LAMBDA_MEMORY_MB,
            timeout=LAMBDA_TIMEOUT_DEFAULT,
            role=self.favorites_lambda_role,
            environment={
                **common_env,
                "MAX_FAVORITES": "50",
                "TEMP_FETCH_TIMEOUT_SECONDS": "5",
            },
            tracing=_lambda.Tracing.ACTIVE,
            description="Manages user favorite locations (CRUD, limit enforcement)",
        )

        # -- Preferences / Unit Conversion Lambda -------------------------
        self.unit_lambda = _lambda.Function(
            self,
            "UnitLambda",
            function_name="weather-app-unit",
            runtime=LAMBDA_RUNTIME,
            handler="src.api.handlers.preferences_handler.handler",
            code=placeholder_code,
            memory_size=LAMBDA_MEMORY_MB,
            timeout=LAMBDA_TIMEOUT_DEFAULT,
            role=self.unit_lambda_role,
            environment={
                **common_env,
                "DEFAULT_TEMP_UNIT": "celsius",
            },
            tracing=_lambda.Tracing.ACTIVE,
            description="Handles temperature unit preferences and conversion",
        )

    # ======================================================================= #
    # API Gateway                                                              #
    # ======================================================================= #

    def _create_api_gateway(self) -> None:
        """
        REST API with Lambda proxy integration and a 5-minute cache stage
        (design.md §Multi-Layer Caching Approach, §API Endpoints).

        Endpoint mapping
        ────────────────
        GET  /locations/search          → location_lambda
        GET  /location/device           → location_lambda
        GET  /weather/current           → weather_lambda
        POST /weather/refresh           → weather_lambda
        GET  /weather/forecast/hourly   → forecast_lambda
        GET  /weather/forecast/daily    → forecast_lambda
        GET  /favorites                 → favorites_lambda
        POST /favorites                 → favorites_lambda
        DELETE /favorites/{location_id} → favorites_lambda
        GET  /preferences/units         → unit_lambda
        PUT  /preferences/units         → unit_lambda
        """
        # Shared log group for API Gateway access logs
        self.api_access_log_group = logs.LogGroup(
            self,
            "ApiAccessLogs",
            log_group_name="/aws/apigateway/weather-app",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.api = apigw.RestApi(
            self,
            "WeatherApi",
            rest_api_name="weather-app-api",
            description="Serverless Weather App REST API",
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                # Enable API Gateway caching (5 min) — design.md §Property 28
                caching_enabled=True,
                cache_ttl=APIGW_CACHE_TTL,
                cache_cluster_enabled=True,
                cache_cluster_size="0.5",
                # Access logging
                access_log_destination=apigw.LogGroupLogDestination(
                    self.api_access_log_group
                ),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                # Enable X-Ray tracing
                tracing_enabled=True,
                # Throttling — design.md §1000 RPS
                throttling_rate_limit=1000,
                throttling_burst_limit=500,
                # Metrics for CloudWatch alarms
                metrics_enabled=True,
                # Detailed CloudWatch metrics per resource/method
                data_trace_enabled=False,  # avoid PII in logs
                logging_level=apigw.MethodLoggingLevel.ERROR,
                # ----------------------------------------------------------------
                # Per-method cache-key configuration — task 10.1
                # ----------------------------------------------------------------
                # Endpoints that should be cached include the relevant query params
                # as cache-key components so different parameter combinations are
                # cached independently.  Mutation endpoints and force-refresh have
                # caching_enabled=False so they always hit Lambda.
                # ----------------------------------------------------------------
                method_options={
                    # GET /locations/search — cache keyed on ?q
                    "~1locations~1search/GET": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_ttl=Duration.seconds(300),
                        cache_key_parameters=[
                            "method.request.querystring.q",
                        ],
                    ),
                    # GET /weather/current — cache keyed on ?lat, ?lon, ?units
                    "~1weather~1current/GET": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_ttl=Duration.seconds(300),
                        cache_key_parameters=[
                            "method.request.querystring.lat",
                            "method.request.querystring.lon",
                            "method.request.querystring.units",
                        ],
                    ),
                    # POST /weather/refresh — always fresh, never cache
                    "~1weather~1refresh/POST": apigw.MethodDeploymentOptions(
                        caching_enabled=False,
                    ),
                    # GET /weather/forecast/hourly — cache keyed on ?lat, ?lon
                    "~1weather~1forecast~1hourly/GET": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_ttl=Duration.seconds(300),
                        cache_key_parameters=[
                            "method.request.querystring.lat",
                            "method.request.querystring.lon",
                        ],
                    ),
                    # GET /weather/forecast/daily — cache keyed on ?lat, ?lon
                    "~1weather~1forecast~1daily/GET": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_ttl=Duration.seconds(300),
                        cache_key_parameters=[
                            "method.request.querystring.lat",
                            "method.request.querystring.lon",
                        ],
                    ),
                    # GET /preferences/units — user-specific, 1-minute private cache
                    "~1preferences~1units/GET": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_ttl=Duration.seconds(60),
                        cache_key_parameters=[
                            "method.request.querystring.user_id",
                        ],
                    ),
                    # PUT /preferences/units — mutation, never cache
                    "~1preferences~1units/PUT": apigw.MethodDeploymentOptions(
                        caching_enabled=False,
                    ),
                    # POST /favorites — mutation, never cache
                    "~1favorites/POST": apigw.MethodDeploymentOptions(
                        caching_enabled=False,
                    ),
                    # DELETE /favorites/{location_id} — mutation, never cache
                    "~1favorites~1{location_id}/DELETE": apigw.MethodDeploymentOptions(
                        caching_enabled=False,
                    ),
                },
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization", "X-Correlation-ID"],
            ),
        )

        # Helper: create a Lambda proxy integration
        def proxy_integration(fn: _lambda.Function) -> apigw.LambdaIntegration:
            return apigw.LambdaIntegration(
                fn,
                proxy=True,
                allow_test_invoke=True,
            )

        location_integration = proxy_integration(self.location_lambda)
        weather_integration = proxy_integration(self.weather_lambda)
        forecast_integration = proxy_integration(self.forecast_lambda)
        favorites_integration = proxy_integration(self.favorites_lambda)
        unit_integration = proxy_integration(self.unit_lambda)

        # ---- /locations/search -------------------------------------------
        locations = self.api.root.add_resource("locations")
        locations_search = locations.add_resource("search")
        locations_search.add_method(
            "GET",
            location_integration,
            request_parameters={
                "method.request.querystring.q": True,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # ---- /location/device -------------------------------------------
        location = self.api.root.add_resource("location")
        location_device = location.add_resource("device")
        location_device.add_method(
            "GET",
            location_integration,
            request_parameters={
                "method.request.querystring.lat": True,
                "method.request.querystring.lon": True,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="404"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # ---- /weather/* --------------------------------------------------
        weather = self.api.root.add_resource("weather")

        # GET /weather/current
        weather_current = weather.add_resource("current")
        weather_current.add_method(
            "GET",
            weather_integration,
            request_parameters={
                "method.request.querystring.lat": True,
                "method.request.querystring.lon": True,
                "method.request.querystring.units": False,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="504"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # POST /weather/refresh
        weather_refresh = weather.add_resource("refresh")
        weather_refresh.add_method(
            "POST",
            weather_integration,
            request_parameters={
                "method.request.querystring.lat": True,
                "method.request.querystring.lon": True,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="504"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # /weather/forecast/*
        weather_forecast = weather.add_resource("forecast")

        # GET /weather/forecast/hourly
        forecast_hourly = weather_forecast.add_resource("hourly")
        forecast_hourly.add_method(
            "GET",
            forecast_integration,
            request_parameters={
                "method.request.querystring.lat": True,
                "method.request.querystring.lon": True,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="504"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # GET /weather/forecast/daily
        forecast_daily = weather_forecast.add_resource("daily")
        forecast_daily.add_method(
            "GET",
            forecast_integration,
            request_parameters={
                "method.request.querystring.lat": True,
                "method.request.querystring.lon": True,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="504"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # ---- /favorites/* -----------------------------------------------
        favorites = self.api.root.add_resource("favorites")
        favorites.add_method(
            "GET",
            favorites_integration,
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="500"),
            ],
        )
        favorites.add_method(
            "POST",
            favorites_integration,
            method_responses=[
                apigw.MethodResponse(status_code="201"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="409"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # DELETE /favorites/{location_id}
        favorite_item = favorites.add_resource("{location_id}")
        favorite_item.add_method(
            "DELETE",
            favorites_integration,
            method_responses=[
                apigw.MethodResponse(status_code="204"),
                apigw.MethodResponse(status_code="404"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

        # ---- /preferences/* ---------------------------------------------
        preferences = self.api.root.add_resource("preferences")
        pref_units = preferences.add_resource("units")
        pref_units.add_method(
            "GET",
            unit_integration,
            request_parameters={
                # user_id is optional but declared so API GW can use it as a cache key
                "method.request.querystring.user_id": False,
            },
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="500"),
            ],
        )
        pref_units.add_method(
            "PUT",
            unit_integration,
            method_responses=[
                apigw.MethodResponse(status_code="200"),
                apigw.MethodResponse(status_code="400"),
                apigw.MethodResponse(status_code="500"),
            ],
        )

    # ======================================================================= #
    # CloudWatch Log Groups & Alarms                                           #
    # ======================================================================= #

    def _create_cloudwatch_resources(self) -> None:
        """
        Create CloudWatch log groups for all Lambda functions and set up
        metric alarms for error rates and latency breaches.

        SNS topic is created for alarm notifications — subscribers (PagerDuty,
        email, etc.) are wired outside the CDK stack.
        """
        # SNS topic for alarm notifications
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name="weather-app-alarms",
            display_name="Weather App Alarms",
        )

        # Lambda functions keyed by friendly name for uniform alarm creation
        lambda_functions: dict[str, _lambda.Function] = {
            "location": self.location_lambda,
            "weather": self.weather_lambda,
            "forecast": self.forecast_lambda,
            "favorites": self.favorites_lambda,
            "unit": self.unit_lambda,
        }

        for name, fn in lambda_functions.items():
            # Explicit log group so we control retention
            log_group = logs.LogGroup(
                self,
                f"{name.capitalize()}LambdaLogGroup",
                log_group_name=f"/aws/lambda/{fn.function_name}",
                retention=LOG_RETENTION,
                removal_policy=RemovalPolicy.DESTROY,
            )

            # Error rate alarm — triggers if ≥5 errors in a 5-minute window
            error_alarm = cloudwatch.Alarm(
                self,
                f"{name.capitalize()}ErrorAlarm",
                alarm_name=f"weather-app-{name}-errors",
                alarm_description=(
                    f"Lambda {name} reported ≥{ALARM_ERROR_RATE_THRESHOLD} "
                    "errors in a 5-minute window"
                ),
                metric=fn.metric_errors(
                    period=Duration.minutes(5),
                    statistic="Sum",
                ),
                threshold=ALARM_ERROR_RATE_THRESHOLD,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            error_alarm.add_alarm_action(
                cw_actions.SnsAction(self.alarm_topic)
            )

            # Latency (p99) alarm — triggers if p99 duration > 5 000 ms
            latency_alarm = cloudwatch.Alarm(
                self,
                f"{name.capitalize()}LatencyAlarm",
                alarm_name=f"weather-app-{name}-latency-p99",
                alarm_description=(
                    f"Lambda {name} p99 duration exceeded "
                    f"{ALARM_LATENCY_THRESHOLD_MS} ms"
                ),
                metric=fn.metric_duration(
                    period=Duration.minutes(5),
                    statistic="p99",
                ),
                threshold=ALARM_LATENCY_THRESHOLD_MS,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            latency_alarm.add_alarm_action(
                cw_actions.SnsAction(self.alarm_topic)
            )

            # Throttle alarm — triggers on any throttles
            throttle_alarm = cloudwatch.Alarm(
                self,
                f"{name.capitalize()}ThrottleAlarm",
                alarm_name=f"weather-app-{name}-throttles",
                alarm_description=f"Lambda {name} throttles detected",
                metric=fn.metric_throttles(
                    period=Duration.minutes(5),
                    statistic="Sum",
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            throttle_alarm.add_alarm_action(
                cw_actions.SnsAction(self.alarm_topic)
            )

        # API Gateway 5xx alarm
        api_5xx_alarm = cloudwatch.Alarm(
            self,
            "Api5xxAlarm",
            alarm_name="weather-app-api-5xx",
            alarm_description="API Gateway returned ≥5 5xx responses in 5 minutes",
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="5XXError",
                dimensions_map={
                    "ApiName": "weather-app-api",
                    "Stage": "v1",
                },
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        api_5xx_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        # API Gateway 4xx alarm (high volume may indicate abuse / bad clients)
        api_4xx_alarm = cloudwatch.Alarm(
            self,
            "Api4xxAlarm",
            alarm_name="weather-app-api-4xx",
            alarm_description="API Gateway returned ≥50 4xx responses in 5 minutes",
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="4XXError",
                dimensions_map={
                    "ApiName": "weather-app-api",
                    "Stage": "v1",
                },
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=50,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        api_4xx_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

    # ======================================================================= #
    # CloudWatch Dashboard                                                     #
    # ======================================================================= #

    def _create_dashboard(self) -> None:
        """
        Operational dashboard with six widget rows:

          Row 1 — Lambda error rates (all 5 functions, stacked)
          Row 2 — Lambda p99 latency
          Row 3 — DynamoDB consumed read/write capacity (all 3 tables)
          Row 4 — API Gateway 4xx / 5xx counts
          Row 5 — Custom WeatherApp cache hit vs miss (current + forecast)
          Row 6 — Custom WeatherApp external API call success/error rates

        Design reference: design.md §Monitoring and Alerting
        Validates: Requirements 8.5
        """
        lambda_names = {
            "location": self.location_lambda,
            "weather": self.weather_lambda,
            "forecast": self.forecast_lambda,
            "favorites": self.favorites_lambda,
            "unit": self.unit_lambda,
        }

        # -- Row 1: Lambda error counts per function (5 min, Sum) ----------
        error_metrics = [
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": fn.function_name},
                period=Duration.minutes(5),
                statistic="Sum",
                label=f"{name} errors",
                color=["#d62728", "#ff7f0e", "#2ca02c", "#1f77b4", "#9467bd"][i],
            )
            for i, (name, fn) in enumerate(lambda_names.items())
        ]

        lambda_errors_widget = cloudwatch.GraphWidget(
            title="Lambda Error Rates",
            left=error_metrics,
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Errors / 5 min", min=0),
        )

        # -- Row 1 (right): Lambda p99 latency ---------------------------------
        latency_metrics = [
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Duration",
                dimensions_map={"FunctionName": fn.function_name},
                period=Duration.minutes(5),
                statistic="p99",
                label=f"{name} p99 ms",
            )
            for name, fn in lambda_names.items()
        ]

        lambda_latency_widget = cloudwatch.GraphWidget(
            title="Lambda p99 Latency",
            left=latency_metrics,
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Duration (ms)", min=0),
        )

        # -- Row 2: DynamoDB consumed capacity --------------------------------
        dynamo_tables = {
            "favorites": self.favorites_table,
            "cache": self.cache_table,
            "preferences": self.preferences_table,
        }

        dynamo_read_metrics = [
            cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ConsumedReadCapacityUnits",
                dimensions_map={"TableName": tbl.table_name},
                period=Duration.minutes(5),
                statistic="Sum",
                label=f"{tbl_name} reads",
            )
            for tbl_name, tbl in dynamo_tables.items()
        ]

        dynamo_write_metrics = [
            cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ConsumedWriteCapacityUnits",
                dimensions_map={"TableName": tbl.table_name},
                period=Duration.minutes(5),
                statistic="Sum",
                label=f"{tbl_name} writes",
            )
            for tbl_name, tbl in dynamo_tables.items()
        ]

        dynamo_widget = cloudwatch.GraphWidget(
            title="DynamoDB Consumed Capacity",
            left=dynamo_read_metrics,
            right=dynamo_write_metrics,
            width=24,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Read CUs", min=0),
            right_y_axis=cloudwatch.YAxisProps(label="Write CUs", min=0),
        )

        # -- Row 3: API Gateway 4xx / 5xx counts ------------------------------
        apigw_4xx = cloudwatch.Metric(
            namespace="AWS/ApiGateway",
            metric_name="4XXError",
            dimensions_map={"ApiName": "weather-app-api", "Stage": "v1"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="4xx errors",
            color="#ff7f0e",
        )
        apigw_5xx = cloudwatch.Metric(
            namespace="AWS/ApiGateway",
            metric_name="5XXError",
            dimensions_map={"ApiName": "weather-app-api", "Stage": "v1"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="5xx errors",
            color="#d62728",
        )
        apigw_count = cloudwatch.Metric(
            namespace="AWS/ApiGateway",
            metric_name="Count",
            dimensions_map={"ApiName": "weather-app-api", "Stage": "v1"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="total requests",
            color="#1f77b4",
        )

        apigw_widget = cloudwatch.GraphWidget(
            title="API Gateway Request Counts",
            left=[apigw_count, apigw_4xx, apigw_5xx],
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Requests / 5 min", min=0),
        )

        # API Gateway latency (p99)
        apigw_latency_widget = cloudwatch.GraphWidget(
            title="API Gateway p99 Latency",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="IntegrationLatency",
                    dimensions_map={"ApiName": "weather-app-api", "Stage": "v1"},
                    period=Duration.minutes(5),
                    statistic="p99",
                    label="integration p99 ms",
                ),
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Latency",
                    dimensions_map={"ApiName": "weather-app-api", "Stage": "v1"},
                    period=Duration.minutes(5),
                    statistic="p99",
                    label="total p99 ms",
                ),
            ],
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Latency (ms)", min=0),
        )

        # -- Row 4: Custom WeatherApp cache hit vs miss -----------------------
        cache_hit_current = cloudwatch.Metric(
            namespace="WeatherApp",
            metric_name="WeatherCacheHit",
            dimensions_map={"data_type": "current"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="cache hit (current)",
            color="#2ca02c",
        )
        cache_miss_current = cloudwatch.Metric(
            namespace="WeatherApp",
            metric_name="WeatherCacheMiss",
            dimensions_map={"data_type": "current"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="cache miss (current)",
            color="#d62728",
        )
        cache_hit_forecast = cloudwatch.Metric(
            namespace="WeatherApp",
            metric_name="WeatherCacheHit",
            dimensions_map={"data_type": "forecast"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="cache hit (forecast)",
            color="#1f77b4",
        )
        cache_miss_forecast = cloudwatch.Metric(
            namespace="WeatherApp",
            metric_name="WeatherCacheMiss",
            dimensions_map={"data_type": "forecast"},
            period=Duration.minutes(5),
            statistic="Sum",
            label="cache miss (forecast)",
            color="#ff7f0e",
        )

        cache_widget = cloudwatch.GraphWidget(
            title="Cache Hit / Miss Ratio",
            left=[cache_hit_current, cache_miss_current, cache_hit_forecast, cache_miss_forecast],
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Count / 5 min", min=0),
        )

        # -- Row 4 (right): External API call success/error -----------------
        ext_api_widget = cloudwatch.GraphWidget(
            title="External API Call Success / Error",
            left=[
                cloudwatch.Metric(
                    namespace="WeatherApp",
                    metric_name="ExternalApiCallCount",
                    dimensions_map={"status": "success"},
                    period=Duration.minutes(5),
                    statistic="Sum",
                    label="external success",
                    color="#2ca02c",
                ),
                cloudwatch.Metric(
                    namespace="WeatherApp",
                    metric_name="ExternalApiCallCount",
                    dimensions_map={"status": "error"},
                    period=Duration.minutes(5),
                    statistic="Sum",
                    label="external error",
                    color="#d62728",
                ),
            ],
            width=12,
            height=6,
            left_y_axis=cloudwatch.YAxisProps(label="Count / 5 min", min=0),
        )

        # -- Assemble dashboard ----------------------------------------------
        self.dashboard = cloudwatch.Dashboard(
            self,
            "WeatherAppDashboard",
            dashboard_name="WeatherApp-Operations",
            widgets=[
                # Row 1: Lambda health
                [lambda_errors_widget, lambda_latency_widget],
                # Row 2: DynamoDB capacity
                [dynamo_widget],
                # Row 3: API Gateway
                [apigw_widget, apigw_latency_widget],
                # Row 4: Custom business metrics
                [cache_widget, ext_api_widget],
            ],
        )

        cdk.CfnOutput(
            self,
            "DashboardUrl",
            value=(
                f"https://console.aws.amazon.com/cloudwatch/home"
                f"#dashboards:name=WeatherApp-Operations"
            ),
            description="CloudWatch operational dashboard URL",
            export_name="WeatherApp-DashboardUrl",
        )

    # ======================================================================= #
    # Stack Outputs                                                            #
    # ======================================================================= #

    def _create_outputs(self) -> None:
        """Export key resource identifiers for downstream scripts and CI/CD."""
        cdk.CfnOutput(
            self,
            "ApiGatewayUrl",
            value=self.api.url,
            description="Base URL of the Weather App REST API",
            export_name="WeatherApp-ApiUrl",
        )
        cdk.CfnOutput(
            self,
            "FavoritesTableName",
            value=self.favorites_table.table_name,
            description="DynamoDB Favorites table name",
            export_name="WeatherApp-FavoritesTable",
        )
        cdk.CfnOutput(
            self,
            "CacheTableName",
            value=self.cache_table.table_name,
            description="DynamoDB Cache table name",
            export_name="WeatherApp-CacheTable",
        )
        cdk.CfnOutput(
            self,
            "PreferencesTableName",
            value=self.preferences_table.table_name,
            description="DynamoDB Preferences table name",
            export_name="WeatherApp-PreferencesTable",
        )
        cdk.CfnOutput(
            self,
            "WeatherApiSecretArn",
            value=self.weather_api_secret.secret_arn,
            description="ARN of the Secrets Manager secret for external API keys",
            export_name="WeatherApp-ApiSecretArn",
        )
        cdk.CfnOutput(
            self,
            "AlarmTopicArn",
            value=self.alarm_topic.topic_arn,
            description="SNS topic ARN for CloudWatch alarm notifications",
            export_name="WeatherApp-AlarmTopicArn",
        )

# Serverless Weather App

A cloud-native weather application built on AWS using serverless architecture and hexagonal (ports and adapters) design patterns.

## Project Structure

```
weather-app/
├── src/
│   ├── domain/              # Domain models and business logic
│   │   ├── __init__.py
│   │   ├── models.py        # Data classes (Location, CurrentConditions, etc.)
│   │   ├── services.py      # Service interfaces / ports
│   │   └── validators.py    # Validation logic
│   ├── application/         # Application services (use cases)
│   │   ├── __init__.py
│   │   ├── location_service.py
│   │   ├── weather_service.py
│   │   ├── forecast_service.py
│   │   ├── favorites_service.py
│   │   └── unit_service.py
│   ├── infrastructure/      # AWS adapters (outbound)
│   │   ├── __init__.py
│   │   ├── aws/
│   │   │   ├── __init__.py
│   │   │   ├── dynamodb_adapter.py
│   │   │   ├── secrets_adapter.py
│   │   │   └── lambda_handler.py
│   │   ├── external/
│   │   │   ├── __init__.py
│   │   │   ├── weather_api_adapter.py
│   │   │   └── geocoding_adapter.py
│   │   └── cache/
│   │       ├── __init__.py
│   │       ├── dynamodb_cache.py
│   │       └── cache_strategy.py
│   └── api/                 # API handlers (inbound adapters)
│       ├── __init__.py
│       ├── handlers/
│       │   ├── __init__.py
│       │   ├── location_handler.py
│       │   ├── weather_handler.py
│       │   ├── forecast_handler.py
│       │   ├── favorites_handler.py
│       │   └── preferences_handler.py
│       └── middleware/
│           ├── __init__.py
│           ├── error_handler.py
│           ├── validation.py
│           └── logging.py
├── tests/
│   ├── __init__.py
│   ├── unit/           # Fast, isolated unit tests
│   │   └── __init__.py
│   ├── integration/    # Tests requiring LocalStack or AWS
│   │   └── __init__.py
│   └── e2e/            # Full end-to-end tests
│       └── __init__.py
├── infrastructure/     # CDK stacks (IaC)
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Architecture

The app follows **hexagonal architecture** (ports and adapters):

- **Domain** — pure Python business logic, no AWS dependencies
- **Application** — orchestrates domain logic and calls out through ports
- **Infrastructure** — concrete adapters for AWS services and external APIs
- **API** — Lambda handlers that translate HTTP events into application calls

## AWS Services

| Service | Purpose |
|---------|---------|
| API Gateway | REST endpoint, caching, rate limiting |
| Lambda (Python 3.12) | Business logic execution |
| DynamoDB | Favorites storage and weather data cache |
| Secrets Manager | External API key storage with auto-rotation |
| CloudWatch | Structured logging and metrics |
| X-Ray | Distributed tracing |

## Getting Started

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run only unit tests
pytest -m unit

# Run property-based tests
pytest -m property

# Run integration tests (requires LocalStack)
pytest -m integration
```

## Test Markers

- `unit` — fast, no external dependencies
- `integration` — requires LocalStack or real AWS
- `e2e` — requires deployed infrastructure
- `property` — Hypothesis property-based tests

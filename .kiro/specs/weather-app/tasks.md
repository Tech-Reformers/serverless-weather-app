# Implementation Plan: Serverless Weather App

## Overview

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Set up AWS infrastructure and project structure
  - [x] 1.1 Create CDK/CloudFormation stack for core AWS services
    - Define AWS CDK Python project with proper structure
    - Create API Gateway REST API with Lambda proxy integration
    - Create DynamoDB tables (favorites, cache, preferences)
    - Create Lambda functions with Python 3.12 runtime
    - Configure Secrets Manager for API keys
    - Set up CloudWatch logs and metrics
    - _Requirements: All infrastructure requirements_

  - [ ]* 1.2 Write infrastructure tests
    - Test CDK stack synthesis
    - Validate resource configurations
    - _Requirements: Infrastructure validation_

  - [x] 1.3 Set up Python project structure
    - Create src/domain, src/application, src/infrastructure, src/api directories
    - Configure requirements.txt with dependencies
    - Set up pytest configuration and test structure
    - _Requirements: Development environment_

- [x] 2. Implement domain models and core interfaces
  - [x] 2.1 Create Python domain models and data classes
    - Implement Location, CurrentConditions, Forecast, FavoriteLocation classes
    - Implement TempUnit enum and validation logic
    - Implement DeviceLocationData and related models
    - _Requirements: 1.1, 2.1, 3.1, 5.1_

  - [ ]* 2.2 Write property test for domain model validation
    - **Property 9: Query Length Validation Invariant**
    - **Validates: Requirements 1.2**

  - [x] 2.3 Implement service interfaces (ports)
    - Create LocationServicePort with search_locations method
    - Create WeatherDataServicePort with get_current_conditions method
    - Create ForecastServicePort with get_hourly_forecast and get_daily_forecast methods
    - Create FavoritesServicePort with add_favorite, remove_favorite, list_favorites methods
    - Create UnitConversionServicePort with convert_temperature and user preference methods
    - _Requirements: 1.1, 2.1, 3.1, 5.1, 6.1, 7.1_

  - [ ]* 2.4 Write unit tests for service interfaces
    - Test interface contracts and method signatures
    - Test error handling scenarios
    - _Requirements: All service requirements_

- [~] 3. Checkpoint - Validate core structure
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement infrastructure adapters (AWS services)
  - [x] 4.1 Implement DynamoDB adapter for data persistence
    - Create DynamoDBFavoritesService implementing FavoritesServicePort
    - Implement atomic counters for favorite limit enforcement (max 50)
    - Implement conditional writes for duplicate prevention
    - Create DynamoDBUnitService for user preferences
    - _Requirements: 5.1, 5.2, 5.3, 5.7, 5.8, 6.4, 6.5_

  - [ ]* 4.2 Write property test for favorite limit enforcement
    - **Property 1: Never Exceed 50 Favorites**
    - **Validates: Requirements 5.1, 5.2**

  - [x] 4.3 Implement DynamoDB cache adapter
    - Create DynamoDB cache for weather data with TTL (15 minutes)
    - Implement cache strategy for current conditions and forecasts
    - Create cache invalidation logic
    - _Requirements: 2.5, 3.1, 3.2, 7.1_

  - [ ]* 4.4 Write property test for cache TTL enforcement
    - **Property 3: Never Cache Data Beyond TTL**
    - **Validates: Requirements 2.5, 3.1, 3.2**

  - [x] 4.5 Implement external API adapters
    - Create WeatherAPIAdapter implementing WeatherDataServicePort
    - Create GeocodingAdapter implementing LocationServicePort
    - Implement retry logic with exponential backoff
    - Implement timeout handling (10 seconds for external calls)
    - _Requirements: 1.1, 1.5, 2.1, 2.6, 2.7, 3.1, 3.2, 3.5, 3.6_

  - [x] 4.6 Implement Secrets Manager adapter
    - Create secure API key retrieval from AWS Secrets Manager
    - Implement rotation handling (90 days)
    - Ensure keys never exposed in logs
    - _Requirements: Security requirements_

- [x] 5. Implement application services
  - [x] 5.1 Implement Location Service application logic
    - Create LocationService with query validation (≥2 chars)
    - Implement search result limiting (max 10 results)
    - Implement device location resolution (within 50km)
    - Add timeout handling (5 seconds search, 30 seconds device)
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 4.1, 4.2, 4.3, 4.4_

  - [ ]* 5.2 Write property test for location search latency
    - **Property 20: Location Search Latency**
    - **Validates: Requirements 1.1**

  - [x] 5.3 Implement Weather Data Service with caching
    - Create CachedWeatherDataService with multi-layer cache strategy
    - Implement current conditions retrieval with cache fallback
    - Add loading indicator support
    - Implement refresh logic with timeout (5 seconds)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 5.4 Write property test for current conditions latency
    - **Property 21: Current Conditions Latency**
    - **Validates: Requirements 1.3, 2.1**

  - [x] 5.5 Implement Forecast Service with caching
    - Create CachedForecastService for hourly/daily forecasts
    - Implement 24-hour hourly forecast retrieval
    - Implement 7-day daily forecast retrieval
    - Add forecast section error handling
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [ ]* 5.6 Write property test for forecast retrieval latency
    - **Property 22: Forecast Retrieval Latency**
    - **Validates: Requirements 3.1, 3.2**

  - [x] 5.7 Implement Favorites Service business logic
    - Add favorite count enforcement (max 50)
    - Implement duplicate prevention
    - Add favorite temperature retrieval with timeout (5 seconds)
    - Implement persistence across sessions
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [ ]* 5.8 Write property test for duplicate favorite behavior
    - **Property 14: Duplicate Favorite Behavior**
    - **Validates: Requirements 5.7**

  - [x] 5.9 Implement Unit Conversion Service
    - Create temperature conversion between Celsius/Fahrenheit
    - Implement user preference persistence
    - Add unit preference fallback (Celsius default)
    - Implement real-time unit conversion without API refresh
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [ ]* 5.10 Write property test for temperature unit validation
    - **Property 2: Never Return Invalid Temperature Unit**
    - **Validates: Requirements 6.3**

- [~] 6. Checkpoint - Validate service implementations
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement API handlers and Lambda functions
  - [x] 7.1 Create Lambda handler for location search
    - Implement /locations/search endpoint handler
    - Add query parameter validation
    - Integrate LocationService
    - Format API Gateway response
    - _Requirements: 1.1, 1.2, 1.4, 1.5_

  - [x] 7.2 Create Lambda handler for current weather
    - Implement /weather/current endpoint handler
    - Add coordinate parameter validation
    - Integrate WeatherDataService
    - Support temperature unit parameter
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 7.3 Create Lambda handler for forecasts
    - Implement /weather/forecast/hourly endpoint
    - Implement /weather/forecast/daily endpoint
    - Integrate ForecastService
    - Add forecast-specific error handling
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 7.4 Create Lambda handler for device location
    - Implement /location/device endpoint
    - Add device location resolution logic
    - Integrate LocationService and WeatherDataService
    - Handle permission denied scenarios
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 7.5 Create Lambda handlers for favorites
    - Implement GET /favorites endpoint (list)
    - Implement POST /favorites endpoint (add)
    - Implement DELETE /favorites/{location_id} endpoint (remove)
    - Integrate FavoritesService
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [x] 7.6 Create Lambda handler for preferences
    - Implement GET /preferences/units endpoint
    - Implement PUT /preferences/units endpoint
    - Integrate UnitConversionService
    - Add preference persistence error handling
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 7.7 Create Lambda handler for refresh
    - Implement POST /weather/refresh endpoint
    - Add force refresh logic bypassing cache
    - Integrate WeatherDataService and ForecastService
    - Implement timeout handling (5 seconds)
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [ ]* 7.8 Write integration tests for API endpoints
    - Test all API endpoints with LocalStack
    - Test error scenarios and edge cases
    - Test authentication and authorization
    - _Requirements: All API requirements_

- [x] 8. Implement middleware and error handling
  - [x] 8.1 Create API Gateway middleware layer
    - Implement request validation middleware
    - Create error handling middleware (formatting)
    - Add logging middleware with correlation IDs
    - Implement CORS and security headers
    - _Requirements: Error handling requirements_

  - [x] 8.2 Implement comprehensive error hierarchy
    - Create WeatherAppError base exception
    - Implement ValidationError, ExternalServiceError, etc.
    - Add error response formatting
    - Implement error logging to CloudWatch
    - _Requirements: Error handling across all requirements_

  - [x] 8.3 Implement retry and timeout strategies
    - Create RetryPolicy with exponential backoff
    - Implement timeout handling for external calls
    - Add circuit breaker pattern for external APIs
    - _Requirements: 1.5, 2.6, 3.5, 4.4, 5.5, 7.3_

  - [ ]* 8.4 Write error handling tests
    - Test error propagation and formatting
    - Test retry logic and timeout scenarios
    - Test circuit breaker behavior
    - _Requirements: Error handling validation_

- [~] 9. Checkpoint - Validate API layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement caching strategy and performance optimizations
  - [x] 10.1 Configure API Gateway caching
    - Set up 5-minute cache for appropriate endpoints
    - Configure cache key patterns
    - Implement cache invalidation headers
    - _Requirements: Performance optimization_

  - [x] 10.2 Implement Lambda memory cache
    - Create in-process LRU cache for user preferences
    - Implement cache warming strategy
    - Add cache statistics and monitoring
    - _Requirements: Performance optimization_

  - [x] 10.3 Optimize DynamoDB operations
    - Implement batch operations for favorite temperatures
    - Add DynamoDB DAX caching layer
    - Optimize query patterns with GSIs
    - _Requirements: 5.4, 5.5_

  - [ ]* 10.4 Write performance tests
    - Test API response times under load
    - Test cache hit ratios and effectiveness
    - Test concurrent user scenarios
    - _Requirements: All performance requirements_

- [x] 11. Implement monitoring and observability
  - [x] 11.1 Configure CloudWatch metrics and alarms
    - Create custom metrics for API usage
    - Set up alarms for error rates and latency
    - Configure dashboards for operational visibility
    - _Requirements: Monitoring requirements_

  - [x] 11.2 Implement X-Ray distributed tracing
    - Enable X-Ray for all Lambda functions
    - Add custom annotations and metadata
    - Configure sampling rates (1%)
    - _Requirements: Distributed tracing_

  - [x] 11.3 Implement structured logging
    - Create JSON-formatted logs with correlation IDs
    - Add log levels and contextual information
    - Implement log aggregation and analysis
    - _Requirements: Logging requirements_

  - [ ]* 11.4 Write monitoring tests
    - Test metric collection and emission
    - Test alarm triggering conditions
    - Test log formatting and correlation
    - _Requirements: Monitoring validation_

- [x] 12. Integration and wiring
  - [x] 12.1 Wire all components together
    - Configure dependency injection for all services
    - Connect adapters to application services
    - Wire API handlers to Lambda functions
    - _Requirements: All integration requirements_

  - [x] 12.2 Configure environment variables and secrets
    - Set up Lambda environment variables
    - Configure Secrets Manager integration
    - Add external API endpoint configuration
    - _Requirements: Configuration management_

  - [x] 12.3 Create deployment configuration
    - Set up AWS SAM or CDK deployment pipeline
    - Configure staging and production environments
    - Implement infrastructure as code validation
    - _Requirements: Deployment requirements_

  - [ ]* 12.4 Write end-to-end integration tests
    - Test complete user flows from API to data storage
    - Test error scenarios across component boundaries
    - Test recovery and fallback mechanisms
    - _Requirements: All end-to-end requirements_

- [~] 13. Final checkpoint - Complete implementation
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability (see requirements.md)
- Checkpoints ensure incremental validation throughout implementation
- Property tests validate universal correctness properties from design.md
- Unit tests validate specific examples and edge cases
- Integration tests ensure component interoperability
- Performance tests validate latency and scaling requirements
- All code follows hexagonal architecture pattern with clear separation of concerns
- AWS services are configured with security best practices and cost optimization
- Python 3.12 is used throughout with async/await patterns for I/O operations
- Error handling is comprehensive with proper logging and monitoring

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "2.1", "2.3"] },
    { "id": 1, "tasks": ["4.1", "4.3", "4.5", "4.6"] },
    { "id": 2, "tasks": ["5.1", "5.3", "5.5", "5.7", "5.9"] },
    { "id": 3, "tasks": ["7.1", "7.2", "7.3", "7.4", "7.5", "7.6", "7.7"] },
    { "id": 4, "tasks": ["8.1", "8.2", "8.3", "10.1", "10.2", "10.3"] },
    { "id": 5, "tasks": ["11.1", "11.2", "11.3", "12.1", "12.2", "12.3"] }
  ]
}
```
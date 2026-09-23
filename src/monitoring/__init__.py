"""
Monitoring package for the Serverless Weather App.

Provides observability utilities:
  - :mod:`.logging_config` — structured JSON logging setup and per-request
    context binding for CloudWatch.
  - :func:`.configure_tracing` — X-Ray SDK initialisation (call once per cold start)
  - :func:`.trace_segment`     — Context manager for named X-Ray subsegments
  - :func:`.add_annotation`    — Add searchable annotation to current segment
  - :func:`.add_metadata`      — Add rich metadata to current segment
"""
from .tracing import add_annotation, add_metadata, configure_tracing, trace_segment

__all__ = [
    "configure_tracing",
    "trace_segment",
    "add_annotation",
    "add_metadata",
]

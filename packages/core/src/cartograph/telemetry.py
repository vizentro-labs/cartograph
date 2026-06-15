"""Optional OpenTelemetry instrumentation for Cartograph.

No-op unless `opentelemetry` is installed (`pip install cartograph[otel]`). Even
when installed, metrics only leave the process if you configure an OpenTelemetry
MeterProvider / OTLP exporter (the standard OTEL_* env vars, or
`opentelemetry-instrument`). Disable entirely with CARTOGRAPH_OTEL=0.

Metrics emitted under meter "cartograph":
  cartograph.cache.hits / .misses / .refused / .invalidations  (counters)
  cartograph.compute.saved_ms / .spent_ms                      (counters, ms)
  cartograph.query.live_ms                                     (histogram, ms)
"""
import os

try:
    from opentelemetry import metrics as _m
    _AVAILABLE = True
except Exception:                       # opentelemetry not installed -> pure no-op
    _AVAILABLE = False


class Telemetry:
    """Thin wrapper that records cache/compute metrics via OTel when available,
    and does nothing otherwise. Safe to call on every query."""

    def __init__(self, enabled=None):
        if enabled is None:
            enabled = os.environ.get("CARTOGRAPH_OTEL", "1") not in ("0", "false", "no", "")
        self.on = bool(_AVAILABLE and enabled)
        if not self.on:
            return
        meter = _m.get_meter("cartograph")
        self._hits = meter.create_counter(
            "cartograph.cache.hits", unit="1", description="Cache hits served")
        self._misses = meter.create_counter(
            "cartograph.cache.misses", unit="1", description="Cache misses (live executions)")
        self._refused = meter.create_counter(
            "cartograph.cache.refused", unit="1", description="Uncacheable queries executed live")
        self._inval = meter.create_counter(
            "cartograph.cache.invalidations", unit="1",
            description="Cached queries invalidated then recomputed")
        self._saved = meter.create_counter(
            "cartograph.compute.saved_ms", unit="ms",
            description="Estimated DB execution time avoided by cache hits")
        self._spent = meter.create_counter(
            "cartograph.compute.spent_ms", unit="ms",
            description="DB execution time spent on live runs")
        self._live = meter.create_histogram(
            "cartograph.query.live_ms", unit="ms", description="Live query execution latency")

    def hit(self, saved_ms):
        if not self.on:
            return
        self._hits.add(1)
        if saved_ms:
            self._saved.add(saved_ms)

    def miss(self, spent_ms, invalidated=False):
        if not self.on:
            return
        self._misses.add(1)
        self._spent.add(spent_ms)
        self._live.record(spent_ms)
        if invalidated:
            self._inval.add(1)

    def refused(self, spent_ms):
        if not self.on:
            return
        self._refused.add(1)
        self._spent.add(spent_ms)
        self._live.record(spent_ms)

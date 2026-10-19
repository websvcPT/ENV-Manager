"""OpenTelemetry logs that cannot carry a value.

FastAPI records an unhandled exception and a request validation failure as OpenTelemetry logs
linked to the trace of the request. The exception attributes it adds hold the exception message
and the formatted stack, and a message can repeat a value (a failed save, a decoding error, a
library error that echoes its input). The processor here runs before any exporter on the provider
and keeps only the attributes of an allow list: the exception class, the route and the number of
validation errors. The record still carries its trace and span ids, so it stays linked to the
request, and it never holds the message, the stack, a request body or a field value.
"""

from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadWriteLogRecord

LOG_ATTRIBUTES = frozenset({"exception.type", "http.route", "fastapi.validation.error_count"})


class ScrubLogRecords(LogRecordProcessor):
    """Drops every attribute of a log record that is not on the allow list."""

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        """Keep the allowed attributes and forget the exception object."""
        record = log_record.log_record
        record.attributes = {
            key: value for key, value in (record.attributes or {}).items() if key in LOG_ATTRIBUTES
        }
        record.exception = None

    def shutdown(self) -> None:
        """Nothing is held, so nothing is released."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Nothing is buffered."""
        return True


def scrubbing_logger_provider() -> LoggerProvider:
    """A logger provider whose first processor is the scrubber.

    Exporters added later (FastAPI adds its OTLP exporter when export is configured) run after
    it, in registration order. FastAPI shuts its own exporter down at exit.
    """
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(ScrubLogRecords())
    return provider

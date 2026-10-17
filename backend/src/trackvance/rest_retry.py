"""GET-only cooperative retry. Delivery POST deliberately never imports this module."""
import time
from datetime import timedelta

from .db import iso, utcnow
from .rest_transport import RestError, request

TRANSIENT = {"REST_TIMEOUT", "REST_DNS_TIMEOUT", "REST_DNS_UNAVAILABLE", "REST_UNAVAILABLE", "REST_RESPONSE_TRUNCATED"}
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def read_request(options, secret, *, deadline, check=None, report=None, transport=None, **kwargs):
    checkpoint, notify = check or (lambda: None), report or (lambda _: None)
    send = transport or request
    maximum = options.get("retries", 0) + 1
    delay = options.get("retry_wait_seconds", 10)
    for attempt in range(1, maximum + 1):
        checkpoint()
        if time.monotonic() >= deadline:
            raise RestError("REST_TOTAL_TIMEOUT", "La adquisición agotó su tiempo máximo total.")
        notify({"attempt": attempt, "maximum_attempts": maximum, "status": "REQUESTING", "at": iso(utcnow()), "next_retry_at": None})
        cause = None
        try:
            result = send(options, secret, "GET", deadline=deadline, check=checkpoint, **kwargs)
            if result[0] == 200:
                notify({"attempt": attempt, "maximum_attempts": maximum, "status": "SUCCEEDED", "at": iso(utcnow()), "next_retry_at": None})
                return result
            if result[0] not in TRANSIENT_HTTP:
                definitive = RestError("REST_HTTP_FAILURE", f"La lectura REST recibió HTTP {result[0]}; este fallo no admite reintento automático.")
                definitive.details = {"http_status": result[0], "attempt": attempt, "maximum_attempts": maximum}
                raise definitive
            cause = "REST_HTTP_TRANSIENT_" + str(result[0])
        except RestError as error:
            if time.monotonic() >= deadline:
                raise RestError("REST_TOTAL_TIMEOUT", "La adquisición agotó su tiempo máximo total.") from None
            if error.code not in TRANSIENT:
                raise
            cause = "REST_REQUEST_TIMEOUT" if error.code in {"REST_TIMEOUT", "REST_DNS_TIMEOUT"} else error.code
        state = {"attempt": attempt, "maximum_attempts": maximum, "cause": cause, "at": iso(utcnow()), "next_retry_at": None}
        if attempt == maximum:
            notify({**state, "status": "EXHAUSTED"})
            failure = RestError("REST_RETRIES_EXHAUSTED" if maximum > 1 else cause, "Reintentos agotados." if maximum > 1 else "La petición REST falló; revisa la causa del intento.")
            failure.details = {"cause": cause, "attempt": attempt, "maximum_attempts": maximum}
            raise failure
        until = time.monotonic() + delay
        notify({**state, "status": "WAITING", "next_retry_at": iso(utcnow() + timedelta(seconds=delay))})
        while True:
            checkpoint()
            moment = time.monotonic()
            if moment >= deadline:
                raise RestError("REST_TOTAL_TIMEOUT", "La adquisición agotó su tiempo máximo total durante la espera.")
            if moment >= until:
                break
            remaining = min(until, deadline) - moment
            time.sleep(min(0.1, remaining))
    raise AssertionError("unreachable")

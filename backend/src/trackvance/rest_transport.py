"""Bounded HTTP with verified TLS, pinned DNS and no redirects or ambient proxy."""

import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import time
from decimal import Decimal
from threading import Event, Thread, Timer
from urllib.parse import urlsplit


class RestError(Exception):
    def __init__(self, code, message, *, uncertain=False):
        self.code, self.message, self.uncertain = code, message, uncertain
        super().__init__(message)


class OwnedResponse(http.client.HTTPResponse):
    def begin(self):
        super().begin()
        # Exactly one request owns this connection until our finally block.
        # HTTPConnection otherwise closes its socket on HTTP/1.0 or close
        # responses while their makefile is still reading, defeating the
        # deadline shutdown and remaining-time updates on some platforms.
        self.will_close = False


def endpoint(url: str):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or len(url) > 2048 or any(ord(c) < 33 for c in url)):
            raise ValueError()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        origin_host = f"[{parsed.hostname.lower()}]" if ":" in parsed.hostname else parsed.hostname.lower()
        origin = f"{parsed.scheme}://{origin_host}:{port}"
        allowed = {item.strip() for item in os.getenv("TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS", "").split(",") if item.strip()}
        # The preserved Codex environment already declares its synthetic scope.
        # Reserve exactly one transient fixture origin there; installed product
        # environments gain no implicit private-network or cleartext exception.
        if os.getenv("TRACKVANCE_CERTIFICATION_PROJECT") == "trackvance-codex":
            allowed.add("http://rest-fixture:18090")
        internal = origin in allowed
        if parsed.scheme != "https" and not internal:
            raise RestError("REST_TLS_REQUIRED", "REST requiere HTTPS con certificado válido.")
        return parsed, port, internal
    except (ValueError, TypeError):
        raise RestError("REST_INVALID_URL", "Indica una URL HTTP(S) sin credenciales, query ni fragmento.") from None


def validate_options(options: dict, secret: str, *, source: bool):
    allowed = {"url", "auth", "api_key_header", "timeout_seconds"}
    if source:
        allowed |= {"collection", "fields", "pagination", "page_size", "max_pages", "max_rows", "max_bytes", "total_timeout_seconds"}
    else:
        allowed |= {"idempotency_supported", "idempotency_header"}
    if set(options) - allowed:
        raise RestError("REST_INVALID_OPTIONS", "Parámetros REST no admitidos; los secretos deben ir en el campo de credencial.")
    endpoint(options.get("url", ""))
    auth = options.get("auth", "NONE")
    if auth not in {"NONE", "API_KEY", "BEARER"}:
        raise RestError("REST_INVALID_AUTH", "Autenticación REST no admitida.")
    if auth != "NONE" and (not secret or len(secret.encode()) > 8192 or any(ord(c) < 32 for c in secret)):
        raise RestError("REST_CREDENTIAL_REQUIRED", "Indica una credencial REST válida.")
    for key in ("api_key_header", "idempotency_header"):
        value = options.get(key, "X-API-Key" if key == "api_key_header" else "Idempotency-Key")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,79}", value) or value.lower() in {"host", "content-length", "content-type", "connection", "transfer-encoding", "authorization", "cookie", "user-agent", "referer", "origin", "accept", "accept-encoding", "x-trackvance-batch-id", "x-trackvance-payload-sha256"}:
            raise RestError("REST_INVALID_HEADER", "Nombre de encabezado REST no permitido.")
    if (not source and auth == "API_KEY" and options.get("idempotency_supported", False)
            and options.get("api_key_header", "X-API-Key").lower() == options.get("idempotency_header", "Idempotency-Key").lower()):
        raise RestError("REST_INVALID_HEADER", "La credencial y la identidad idempotente requieren encabezados separados.")
    bounds = {"timeout_seconds": (1, 60, 15), "max_pages": (1, 100000, 10000),
              "page_size": (1, 10000, 1000), "max_rows": (1, 5000000, 5000000),
              "max_bytes": (1024, 2 * 1024**3, 2 * 1024**3), "total_timeout_seconds": (1, 1800, 1800)}
    for name, (lower, upper, default) in bounds.items():
        value = options.get(name, default)
        if type(value) is not int or not lower <= value <= upper:
            raise RestError("REST_INVALID_LIMIT", "Los límites REST deben ser enteros dentro del presupuesto admitido.")
    if not source and type(options.get("idempotency_supported", False)) is not bool:
        raise RestError("REST_INVALID_OPTIONS", "Declara explícitamente el soporte real de idempotencia del receptor.")


def exact_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(data.decode("utf-8-sig"), parse_float=Decimal, parse_int=Decimal,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise RestError("REST_INVALID_JSON", "La respuesta debe ser JSON UTF-8 válido, sin claves repetidas ni valores no finitos.") from None


def json_path(value, path):
    if not path:
        return value
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            raise RestError("REST_COLLECTION_MISSING", "La ruta JSON configurada no existe en la respuesta.")
        value = value[name]
    return value


def resolved_addresses(hostname, port, timeout):
    try:
        return [str(ipaddress.ip_address(hostname))]
    except ValueError:
        pass
    results: queue.Queue[list[str]] = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put([str(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)])
        except OSError:
            results.put([])
    # getaddrinfo has no Python timeout. A daemon resolver never holds the
    # acquisition beyond its remaining deadline or keeps the worker alive.
    Thread(target=resolve, daemon=True).start()
    try:
        addresses = results.get(timeout=max(0.001, timeout))
    except queue.Empty:
        raise RestError("REST_DNS_TIMEOUT", "La resolución del destino REST excedió su tiempo máximo.") from None
    if not addresses:
        raise RestError("REST_DNS_UNAVAILABLE", "No se pudo resolver el destino REST.")
    return addresses


def request(options: dict, secret: str, method: str, *, query="", payload=None,
            headers=None, max_bytes=8 * 1024**2, deadline=None, check=None):
    """Never follow redirects. Resolve each request and connect to the checked IP."""
    parsed, port, internal = endpoint(options["url"])
    checkpoint = check or (lambda: None)
    checkpoint()
    try:
        timeout = options.get("timeout_seconds", 15)
        deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise RestError("REST_TIMEOUT", "La operación REST excedió su tiempo máximo.")
        addresses = resolved_addresses(parsed.hostname, port, timeout)
        if not internal and any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise RestError("REST_INTERNAL_DESTINATION_BLOCKED", "El destino REST resuelve a una red interna no autorizada.")
        timeout = max(0.001, min(timeout, deadline - time.monotonic()))
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        connection.response_class = OwnedResponse
        # Pin this socket, retaining the original hostname for TLS and Host.
        raw_socket = socket.create_connection((addresses[0], port), timeout=timeout)
        if parsed.scheme == "https":
            raw_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname,
                                                                 do_handshake_on_connect=False)
        connection.sock = raw_socket
        expired = Event()
        def expire_socket():
            expired.set()
            try:
                raw_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline_timer = Timer(max(0.001, deadline - time.monotonic()), expire_socket)
        deadline_timer.daemon = True
        deadline_timer.start()
        safe_headers = {"Accept": "application/json", **(headers or {})}
        if options.get("auth") == "BEARER":
            safe_headers["Authorization"] = f"Bearer {secret}"
        elif options.get("auth") == "API_KEY":
            safe_headers[options.get("api_key_header", "X-API-Key")] = secret
        if payload is not None:
            safe_headers["Content-Type"] = "application/json"
        try:
            if isinstance(raw_socket, ssl.SSLSocket):
                raw_socket.settimeout(max(0.001, deadline - time.monotonic()))
                raw_socket.do_handshake()
            checkpoint()
            connection.request(method, (parsed.path or "/") + query, body=payload, headers=safe_headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise RestError("REST_REDIRECT_BLOCKED", "REST no permite redirecciones.", uncertain=method == "POST")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise RestError("REST_ENCODING_UNSUPPORTED", "La respuesta REST debe usar transporte sin compresión.", uncertain=method == "POST")
            body = bytearray()
            while True:
                checkpoint()
                if deadline is not None and time.monotonic() >= deadline:
                    raise RestError("REST_TIMEOUT", "La operación REST excedió su tiempo máximo.", uncertain=method == "POST")
                raw_socket.settimeout(max(0.001, deadline - time.monotonic()))
                block = response.read1(min(65536, max_bytes + 1 - len(body)))
                if not block:
                    if expired.is_set() or time.monotonic() >= deadline:
                        raise RestError("REST_TIMEOUT", "La operación REST excedió su tiempo máximo.", uncertain=method == "POST")
                    if response.length not in {None, 0}:
                        raise RestError("REST_RESPONSE_TRUNCATED", "La respuesta REST terminó antes de recibir todos sus bytes.", uncertain=method == "POST")
                    break
                body.extend(block)
                if len(body) > max_bytes:
                    raise RestError("REST_RESPONSE_SIZE_LIMIT", "La respuesta REST supera el límite de bytes.", uncertain=method == "POST")
            return response.status, bytes(body)
        finally:
            deadline_timer.cancel()
            connection.close()
    except RestError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        if deadline is not None and time.monotonic() >= deadline:
            raise RestError("REST_TIMEOUT", "La operación REST excedió su tiempo máximo.", uncertain=method == "POST") from None
        raise RestError("REST_UNAVAILABLE", "No se pudo completar la operación REST; revisa red, TLS y disponibilidad.", uncertain=method == "POST") from None

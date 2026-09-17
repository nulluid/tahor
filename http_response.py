"""Bound model-response size and elapsed reading time without logging payloads."""
import time

MODEL_RESPONSE_BYTES = 1024 * 1024
MODEL_RESPONSE_SECONDS = 90


def read_bounded(response, deadline, max_bytes=MODEL_RESPONSE_BYTES):
    """Read available chunks, checking a monotonic deadline between reads.

    HTTPResponse.read(n) can wait for n bytes while a provider drips whitespace.
    read1(n) returns available body data instead. Keep a socket timeout on urlopen:
    an individual blocked read can still extend this deadline by that timeout.
    This is an elapsed body-read bound, not a hard cancellation of DNS/TLS or
    HTTP framing. The response owner must close it on success and failure.
    """
    if max_bytes <= 0:
        raise ValueError('Response byte limit must be positive')
    chunks = []
    size = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('Model response deadline exceeded')
        chunk = response.read1(min(65536, max_bytes + 1 - size))
        if time.monotonic() >= deadline:
            raise TimeoutError('Model response deadline exceeded')
        if not isinstance(chunk, bytes):
            raise ValueError('Invalid model response bytes')
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise ValueError('Model response exceeds byte limit')
        chunks.append(chunk)

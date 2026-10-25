"""In-memory multipart parsing.

Starlette's built-in form parser spools large uploads to a temporary file on disk.
Uploaded photos must never touch the disk, so this parses the request body
ourselves and keeps every part in memory, enforcing the size cap while streaming.
"""

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

# Room for the non-file form fields and multipart framing on top of the image itself.
_FIELD_OVERHEAD = 64 * 1024
_MAX_FIELD_BYTES = 1024


class UploadError(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        self.status, self.error, self.detail = status, error, detail


async def read_form(request: Request, max_file_bytes: int) -> tuple[dict[str, str], dict[str, bytes]]:
    """Return (text fields, file fields) from a multipart/form-data request."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise UploadError(415, "unsupported_media_type", "expected multipart/form-data")

    max_body = max_file_bytes + _FIELD_OVERHEAD
    too_large = UploadError(413, "too_large", f"upload exceeds {max_file_bytes // (1024 * 1024)} MB")
    try:
        if int(request.headers.get("content-length", 0)) > max_body:
            raise too_large
    except ValueError:
        raise UploadError(400, "invalid_request", "bad Content-Length") from None

    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    state: dict = {}

    def on_part_begin():
        state.update(headers={}, field=b"", value=b"", data=bytearray())

    def on_header_field(data, start, end):
        state["field"] += data[start:end]

    def on_header_value(data, start, end):
        state["value"] += data[start:end]

    def on_header_end():
        state["headers"][state["field"].lower()] = state["value"]
        state["field"], state["value"] = b"", b""

    def on_part_data(data, start, end):
        state["data"] += data[start:end]

    def on_part_end():
        _, disp = parse_options_header(state["headers"].get(b"content-disposition", b""))
        name = disp.get(b"name", b"").decode("utf-8", "replace")
        if not name:
            return
        if b"filename" in disp:
            if len(state["data"]) > max_file_bytes:
                raise too_large
            files[name] = bytes(state["data"])
        else:
            if len(state["data"]) > _MAX_FIELD_BYTES:
                raise UploadError(413, "too_large", f"form field {name!r} is too long")
            fields[name] = state["data"].decode("utf-8", "replace")
        state["data"] = bytearray()

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body:
                raise too_large
            parser.write(chunk)
        parser.finalize()
    except UploadError:
        raise
    except Exception:
        raise UploadError(400, "invalid_request", "malformed multipart body") from None
    finally:
        state.clear()
    return fields, files

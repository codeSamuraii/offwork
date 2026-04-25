"""Image thumbnailing service: HTTP upload -> remote resize -> response.

Resizing is CPU-bound (Pillow), embarrassingly parallel, and stateless --
a textbook pyfuse task.  The traced entry point delegates to two plain
helpers (``_decode``, ``_encode_jpeg``) that pyfuse picks up via AST
analysis.  Only ``thumbnail`` carries ``@trace``.

Endpoints:
    POST /thumbnails   -- multipart upload, returns JPEG bytes

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    uvicorn examples.image_thumbnails:app --reload

    curl -X POST localhost:8000/thumbnails \\
         -F 'file=@photo.jpg' -F 'size=256' -o thumb.jpg
"""

from io import BytesIO

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps

import pyfuse
from pyfuse import trace


# --- helpers (auto-discovered) --------------------------------------------

def _decode(blob: bytes) -> Image.Image:
    img = Image.open(BytesIO(blob))
    # Honour EXIF orientation so phone pictures aren't rotated.
    return ImageOps.exif_transpose(img).convert("RGB")


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


# --- entry point ----------------------------------------------------------

@trace(timeout=15, retries=1)
def thumbnail(image_bytes: bytes, size: int = 256, quality: int = 85) -> bytes:
    """Resize *image_bytes* to fit a *size*x*size* box, return JPEG bytes."""
    img = _decode(image_bytes)
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return _encode_jpeg(img, quality)


# --- FastAPI app ----------------------------------------------------------

app = FastAPI(title="pyfuse thumbnail service")


@app.on_event("startup")
async def _startup() -> None:
    pyfuse.connect("redis://localhost:6379")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await pyfuse.disconnect()


@app.post("/thumbnails")
async def make_thumbnail(
    file: UploadFile = File(...),
    size: int = Form(256),
) -> Response:
    blob = await file.read()
    jpeg = await thumbnail.run(blob, size=size)
    return Response(content=jpeg, media_type="image/jpeg")

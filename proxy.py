import hmac
import hashlib
import os

from io import BytesIO
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from PIL import Image

# Default user-agent for the HTTP client
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0")

# Debug mode
DEBUG = os.getenv("DEBUG", "false").lower() in ("yes", "true", "t", "1")

# Initialize FastAPI app
app = FastAPI(title="Image Proxy", debug=DEBUG)

# Secret key for request validation
secret_key = hashlib.sha256()
secret_key.update(os.getenv("BOT_TOKEN", "").encode())


def get_check_string(url: str, fallback_url: Optional[str] = None, size: Optional[int] = None) -> str:
    check_string = url

    if fallback_url:
        check_string += f":{fallback_url}"

    if size:
        check_string += f":{size}"

    return check_string


async def download_image(url: str, fallback_url: Optional[str] = None) -> bytes:
    # Set request headers
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Sec-GPC": "1",
        "DNT": "1",
    }

    async with AsyncClient(headers=request_headers, timeout=30) as client:
        try:
            async with client.stream("GET", url, follow_redirects=True) as response:
                content_length = response.headers.get("Content-Length")

                # Check if the image exists
                if response.status_code != 200:
                    raise HTTPException(status_code=404, detail="Image not found")

                # Check image size
                if content_length and content_length.isdigit() and int(content_length) > 5 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Image too large")

                # Check content type
                if not response.headers.get("Content-Type", "").startswith("image/"):
                    raise HTTPException(status_code=415, detail="Unsupported content type")

                return await response.aread()
        except HTTPException as e:
            # Attempt to download from fallback URL if provided
            if fallback_url:
                return await download_image(fallback_url)
            else:
                raise e


@app.get("/proxy")
async def proxy_image(
    url: str,
    hash: str = Query(alias="h"),
    size: int = Query(None, alias="s", le=1024),
    fallback_url: str = Query(None, alias="f"),
):
    expected_hash = hmac.new(secret_key.digest(), get_check_string(url, fallback_url).encode(), hashlib.sha256).hexdigest()

    if hash != expected_hash:
        raise HTTPException(status_code=403, detail="Invalid request")

    image_data = await download_image(url, fallback_url)

    # Check image format
    try:
        image = Image.open(BytesIO(image_data))
    except Exception:
        raise HTTPException(status_code=415, detail="Unsupported image format")

    # Convert image to RGB
    image = image.convert("RGB")

    # Resize image if `s` parameter is provided
    if size:
        image.thumbnail((size, size))

    # Save the image to a byte array
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="JPEG", quality=80)
    img_byte_arr.seek(0)

    return StreamingResponse(img_byte_arr, media_type="image/jpeg")


if DEBUG:

    @app.get("/hash")
    async def get_hash(url: str, fallback_url: str = Query(None)):
        # Generate the hash for the given URL
        hash_value = hmac.new(secret_key.digest(), get_check_string(url, fallback_url).encode(), hashlib.sha256).hexdigest()
        return {"url": url, "fallback_url": fallback_url, "hash": hash_value}


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run(app)
    except KeyboardInterrupt:
        pass

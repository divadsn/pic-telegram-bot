import hmac
import hashlib

from io import BytesIO
from os import getenv

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from PIL import Image
from redis import asyncio as aioredis

# Default user-agent for the HTTP client
USER_AGENT = getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36")

# Debug mode
DEBUG = getenv("DEBUG", "false").lower() in ("yes", "true", "t", "1")

# Initialize FastAPI app
app = FastAPI(title="Image Proxy", debug=DEBUG)

# Redis for caching
redis = aioredis.from_url(getenv("REDIS_URL", "redis://localhost"))

# Secret key for validation
secret_key = hashlib.sha256()
secret_key.update(getenv("BOT_TOKEN", "").encode())


async def download_image(url: str) -> bytes:
    # Set request headers
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/webp,*/*",
        "Accept-Encoding": "gzip, deflate",
        "Sec-GPC": "1",
        "DNT": "1",
    }

    async with AsyncClient(headers=request_headers, timeout=30) as client:
        response = await client.get(url, follow_redirects=True)

        # Check if the image exists
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="Image not found")

        # Check image size
        if len(response.content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Image too large")

        return response.content


async def cache_image(url_hash: str, image: Image):
    # Save image as jpeg with 80% quality
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="JPEG", quality=80)
    img_byte_arr.seek(0)

    # Cache the image for 1 hour
    await redis.set(f"image_cache:{url_hash}", img_byte_arr.getvalue(), ex=3600)


@app.get("/proxy")
async def proxy_image(background_tasks: BackgroundTasks, url: str, h: str, s: int = Query(None, le=1024)):
    # Validate the request using HMAC
    expected_hash = hmac.new(secret_key.digest(), url.encode(), hashlib.sha256).hexdigest()

    if h != expected_hash:
        raise HTTPException(status_code=403, detail="Invalid request")

    # Generate a hash of the URL for the cache key
    url_hash = hashlib.sha256(url.encode()).hexdigest()
    cached_image = await redis.get(f"image_cache:{url_hash}")

    # Return the cached image if it exists
    if cached_image and s is None:
        headers = {"Cache-Control": "public, max-age=3600"}
        return StreamingResponse(BytesIO(cached_image), media_type="image/jpeg", headers=headers)

    if cached_image:
        image = Image.open(BytesIO(cached_image))
    else:
        image_data = await download_image(url)

        # Check image format
        try:
            image = Image.open(BytesIO(image_data))
        except Exception:
            raise HTTPException(status_code=415, detail="Unsupported image format")

        # Convert image to RGB
        image = image.convert("RGB")
        background_tasks.add_task(cache_image, url_hash, image.copy())

    # Resize image if `s` parameter is provided
    if s:
        image.thumbnail((s, s))

    # Save the image to a byte array
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="JPEG", quality=80)
    img_byte_arr.seek(0)

    headers = {"Cache-Control": "public, max-age=3600"}
    return StreamingResponse(img_byte_arr, media_type="image/jpeg", headers=headers)


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run(app)
    except KeyboardInterrupt:
        pass

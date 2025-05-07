import hashlib
import hmac
import logging
import os

from urllib.parse import quote_plus, urlparse
from uuid import uuid4

from async_lru import alru_cache
from bs4 import BeautifulSoup
from httpx import AsyncClient, HTTPError

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.handlers import InlineQueryHandler
from pyrogram.types import InlineQueryResultPhoto, InlineQuery

# enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SEARCH_URL = "https://znajdz.se/search"
PROXY_URL = "https://bot.znajdz.se/proxy?url={0}&h={1}"


class PictureSearchBot:
    def __init__(self, api_id, api_hash, bot_token):
        self.client = Client("bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
        self.client.add_handler(InlineQueryHandler(self.inline_query_handler))

        self.secret_key = hashlib.sha256()
        self.secret_key.update(bot_token.encode())

    @alru_cache(ttl=3600)
    async def search_images(self, query: str):
        query_url = SEARCH_URL.format(quote_plus(query.strip()))
        images = []

        async with AsyncClient(timeout=30) as client:
            response = await client.get(
                query_url,
                params={
                    "q": query,
                    "category_images": "",
                    "language": "auto",
                    "time_range": "",
                    "safesearch": 1,
                    "theme": "simple",
                    "disabled_engines": "artic__images,openverse__images,deviantart__images,flickr__images,library of congress__images,pinterest__images,unsplash__images,wikicommons.images__images,wallhaven__images",
                })

            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # Get all image elements from element with id 'urls'
            for i, result in enumerate(soup.find("div", id="urls").find_all("article")):
                image_url = result.find("a", class_="result-images-source").find("img")["data-src"]
                resolution = result.find("span", class_="image_resolution")

                # Try to filter out svg images by parsing the url path and checking the extension
                path = urlparse(image_url).path
                if os.path.splitext(path)[1].lower() == ".svg":
                    continue

                if resolution:
                    if "x" in resolution.text:
                        image_size = resolution.text.split("x")
                    elif "×" in resolution.text:
                        image_size = resolution.text.split("×")
                else:
                    image_size = ("100", "100")

                images.append(
                    InlineQueryResultPhoto(
                        id=str(uuid4()),
                        photo_url=self.get_url(image_url),
                        photo_width=int(image_size[0]),
                        photo_height=int(image_size[1]),
                        thumb_url=self.get_url(image_url, 300),
                        caption=f"[View Image](<{image_url}>)",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                )

        return images

    async def inline_query_handler(self, bot: Client, update: InlineQuery):
        query = update.query
        offset = int(update.offset) if update.offset else 0

        if query:
            try:
                results = await self.search_images(query)
            except HTTPError:
                results = []

            next_offset = offset + 50 if len(results) > offset + 50 else ""
            await update.answer(results[offset:offset + 50], is_gallery=True, next_offset=str(next_offset))

    def get_url(self, image_url: str, size: int|None = None) -> str:
        url_hash = hmac.new(self.secret_key.digest(), image_url.encode(), hashlib.sha256).hexdigest()
        new_url = PROXY_URL.format(quote_plus(image_url), url_hash)

        if size:
            new_url += f"&s={size}"

        return new_url

    def run(self):
        self.client.run()


if __name__ == "__main__":
    bot = PictureSearchBot(api_id=os.getenv("API_ID"), api_hash=os.getenv("API_HASH"), bot_token=os.getenv("BOT_TOKEN"))
    bot.run()

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::{Router, routing::get};
use hmac::{Hmac, Mac};
use image::ImageFormat;
use redis::AsyncCommands;
use reqwest::Client;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone)]
struct AppState {
    secret_key: Vec<u8>,
    redis: redis::Client,
    http_client: Client,
    user_agent: String,
}

#[derive(Deserialize)]
struct ProxyParams {
    url: String,
    h: String,
    s: Option<u32>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let bot_token = std::env::var("BOT_TOKEN").unwrap_or_default();
    let redis_url = std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://localhost".to_string());
    let user_agent = std::env::var("USER_AGENT").unwrap_or_else(|_| {
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36".to_string()
    });
    let host = std::env::var("HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port = std::env::var("PORT").unwrap_or_else(|_| "8000".to_string());

    // Derive the secret key: SHA-256(BOT_TOKEN)
    let secret_key = Sha256::digest(bot_token.as_bytes()).to_vec();

    let redis = redis::Client::open(redis_url).expect("Failed to connect to Redis");

    let http_client = Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .expect("Failed to build HTTP client");

    let state = AppState { secret_key, redis, http_client, user_agent };

    let app = Router::new().route("/proxy", get(proxy_image)).with_state(state);

    let addr = format!("{host}:{port}");
    tracing::info!("Listening on {addr}");
    let listener = TcpListener::bind(&addr).await.expect("Failed to bind");
    axum::serve(listener, app).await.expect("Server error");
}

async fn proxy_image(
    State(state): State<AppState>,
    Query(params): Query<ProxyParams>,
) -> Response {
    // Validate the `s` parameter: must be 1..=1024
    if let Some(s) = params.s {
        if s == 0 || s > 1024 {
            return (StatusCode::UNPROCESSABLE_ENTITY, "s must be between 1 and 1024").into_response();
        }
    }

    // Validate HMAC: HMAC-SHA256(secret_key, url)
    let mut mac = HmacSha256::new_from_slice(&state.secret_key)
        .expect("HMAC accepts any key length");
    mac.update(params.url.as_bytes());
    let expected = hex::encode(mac.finalize().into_bytes());

    if params.h != expected {
        return (StatusCode::FORBIDDEN, "Invalid request").into_response();
    }

    // Compute the URL hash for the Redis cache key
    let url_hash = hex::encode(Sha256::digest(params.url.as_bytes()));
    let cache_key = format!("image_cache:{url_hash}");

    let mut redis_conn = match state.redis.get_multiplexed_async_connection().await {
        Ok(conn) => conn,
        Err(e) => {
            tracing::error!("Redis connection error: {e}");
            return (StatusCode::INTERNAL_SERVER_ERROR, "Cache unavailable").into_response();
        }
    };

    // Try to get cached image
    let cached: Option<Vec<u8>> = match redis_conn.get(&cache_key).await {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("Redis GET error: {e}");
            None
        }
    };

    if let Some(cached_data) = cached {
        if params.s.is_none() {
            // Return cached image as-is
            return jpeg_response(cached_data);
        }

        // Resize the cached image
        let img = match image::load_from_memory_with_format(&cached_data, ImageFormat::Jpeg) {
            Ok(img) => img,
            Err(_) => return (StatusCode::UNSUPPORTED_MEDIA_TYPE, "Unsupported image format").into_response(),
        };
        let resized = img.thumbnail(params.s.unwrap(), params.s.unwrap());
        return match encode_jpeg(&resized, 80) {
            Ok(bytes) => jpeg_response(bytes),
            Err(_) => (StatusCode::INTERNAL_SERVER_ERROR, "Failed to encode image").into_response(),
        };
    }

    // Download and process image
    let image_data = match download_image(&state.http_client, &params.url, &state.user_agent).await {
        Ok(data) => data,
        Err(resp) => return resp,
    };

    let img = match image::load_from_memory(&image_data) {
        Ok(img) => img,
        Err(_) => return (StatusCode::UNSUPPORTED_MEDIA_TYPE, "Unsupported image format").into_response(),
    };

    // Convert to RGB
    let img = image::DynamicImage::ImageRgb8(img.into_rgb8());

    // Encode the full-size image for caching
    let base_jpeg = match encode_jpeg(&img, 80) {
        Ok(bytes) => bytes,
        Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Failed to encode image").into_response(),
    };

    // Cache in background (best-effort)
    let mut bg_conn = redis_conn.clone();
    let bg_key = cache_key.clone();
    let bg_bytes = base_jpeg.clone();
    tokio::spawn(async move {
        let _: Result<(), _> = bg_conn.set_ex(&bg_key, bg_bytes, 3600).await;
    });

    // If resize is requested, thumbnail the image and re-encode; otherwise return the base JPEG
    let jpeg_bytes = if let Some(size) = params.s {
        let resized = img.thumbnail(size, size);
        match encode_jpeg(&resized, 80) {
            Ok(bytes) => bytes,
            Err(_) => return (StatusCode::INTERNAL_SERVER_ERROR, "Failed to encode image").into_response(),
        }
    } else {
        base_jpeg
    };

    jpeg_response(jpeg_bytes)
}

fn encode_jpeg(img: &image::DynamicImage, quality: u8) -> Result<Vec<u8>, image::ImageError> {
    let mut buf = Vec::new();
    let encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, quality);
    img.write_with_encoder(encoder)?;
    Ok(buf)
}

fn jpeg_response(bytes: Vec<u8>) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, "image/jpeg".parse().unwrap());
    headers.insert(header::CACHE_CONTROL, "public, max-age=3600".parse().unwrap());
    (StatusCode::OK, headers, bytes).into_response()
}

async fn download_image(client: &Client, url: &str, user_agent: &str) -> Result<Vec<u8>, Response> {
    let response = client
        .get(url)
        .header("User-Agent", user_agent)
        .header("Accept", "image/webp,*/*")
        .header("Accept-Encoding", "gzip, deflate")
        .header("Sec-GPC", "1")
        .header("DNT", "1")
        .send()
        .await
        .map_err(|_| (StatusCode::NOT_FOUND, "Image not found").into_response())?;

    if response.status() != reqwest::StatusCode::OK {
        return Err((StatusCode::NOT_FOUND, "Image not found").into_response());
    }

    // Check content-length
    if let Some(len) = response.content_length() {
        if len > 5 * 1024 * 1024 {
            return Err((StatusCode::PAYLOAD_TOO_LARGE, "Image too large").into_response());
        }
    }

    // Check content-type
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !content_type.starts_with("image/") {
        return Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, "Unsupported content type").into_response());
    }

    let bytes = response
        .bytes()
        .await
        .map_err(|_| (StatusCode::BAD_GATEWAY, "Failed to read image").into_response())?;

    Ok(bytes.to_vec())
}

# Stage 1: Build Python dependencies
FROM python:3.12-slim AS python-build
LABEL maintainer="David Sn <divad.nnamtdeis@gmail.com>"

# Install build essentials
RUN apt-get update && apt-get install -y build-essential

# Set the working directory
WORKDIR /app

# Copy the pyproject.toml and poetry.lock files
COPY pyproject.toml poetry.lock ./

# Install Poetry
RUN pip install poetry

# Install bot dependencies and create a virtual environment in /app/.venv
RUN poetry config virtualenvs.in-project true && poetry install --no-root

# Stage 2: Build the Rust proxy binary
FROM rust:slim AS rust-build
LABEL maintainer="David Sn <divad.nnamtdeis@gmail.com>"

# Install build essentials for ring/openssl
RUN apt-get update && apt-get install -y pkg-config

WORKDIR /app

# Copy the proxy Rust source
COPY proxy ./proxy

# Build in release mode
RUN cargo build --release --manifest-path proxy/Cargo.toml

# Stage 3: Final image
FROM python:3.12-slim
LABEL maintainer="David Sn <divad.nnamtdeis@gmail.com>"

# Set the working directory
WORKDIR /app

# Copy the virtual environment from the Python build stage
COPY --from=python-build /app/.venv /app/.venv

# Copy the Rust proxy binary from the Rust build stage
COPY --from=rust-build /app/proxy/target/release/proxy /app/proxy-bin

# Copy the rest of the application code
COPY . .

# Add the virtual environment to the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Run the bot by default
CMD ["python", "bot.py"]

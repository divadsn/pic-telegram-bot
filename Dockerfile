# Stage 1: Build stage
FROM python:3.12-slim AS build
LABEL maintainer="David Sn <divad.nnamtdeis@gmail.com>"

# Install build essentials
RUN apt-get update && apt-get install -y build-essential

# Set the working directory
WORKDIR /app

# Copy the pyproject.toml and poetry.lock files
COPY pyproject.toml poetry.lock ./

# Install Poetry
RUN pip install poetry

# Install dependencies including the proxy group and create a virtual environment in /app/.venv
RUN poetry config virtualenvs.in-project true && poetry install --with proxy --no-root

# Stage 2: Final stage
FROM python:3.12-slim
LABEL maintainer="David Sn <divad.nnamtdeis@gmail.com>"

# Set the working directory
WORKDIR /app

# Copy the virtual environment from the build stage
COPY --from=build /app/.venv /app/.venv

# Copy the rest of the application code
COPY . .

# Add the virtual environment to the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Run the bot
CMD ["python", "bot.py"]
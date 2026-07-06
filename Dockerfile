FROM node:22-alpine AS ui
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=ui /src/runrail/web/static ./src/runrail/web/static
RUN pip install --no-cache-dir .
ENV RUNRAIL_HOME=/data RUNRAIL_HOST=0.0.0.0
VOLUME /data
EXPOSE 8080
CMD ["runrail", "serve", "--host", "0.0.0.0", "--port", "8080"]


# frontend/Dockerfile — Athena Frontend (production)
#
# Multi-stage build:
#   1. Node.js — build the Vite + React app
#   2. Nginx   — serve static files with SPA routing

# ── Build stage ──────────────────────────────────────────────────────
FROM node:26-alpine AS builder

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── Runtime stage ────────────────────────────────────────────────────
FROM nginx:alpine

# Copy built assets
COPY --from=builder /build/dist/ /usr/share/nginx/html/

# Nginx config for SPA (all routes → index.html)
RUN echo 'server { \
    listen 80; \
    server_name _; \
    root /usr/share/nginx/html; \
    index index.html; \
    location / { \
        try_files $uri $uri/ /index.html; \
    } \
    location /assets/ { \
        expires 1y; \
        add_header Cache-Control "public, immutable"; \
    } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

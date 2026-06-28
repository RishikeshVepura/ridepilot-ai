# RidePilot AI — Frontend (Next.js) dev image
#
# Mirrors the backend services' dev setup: dependencies are installed into the
# image at build time, and docker-compose.override.yml bind-mounts the source
# for live reload (Next.js dev server watches and recompiles on change). A
# rebuild is only needed when package.json changes.
FROM node:20-slim

WORKDIR /app

# Install dependencies first so this layer is cached unless package.json changes.
# No package-lock.json is committed, so use `npm install`.
COPY package.json ./
RUN npm install

# Copy the rest of the app (overlaid by the bind mount in dev).
COPY . .

EXPOSE 3000

# Bind to 0.0.0.0 so the dev server is reachable through the published port.
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0"]

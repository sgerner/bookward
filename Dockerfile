FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
WORKDIR /app
COPY package*.json .npmrc ./
RUN npm ci
COPY tsconfig.json vite.config.ts server.js ./
COPY static ./static
COPY src ./src
RUN npm run check && npm run build

FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS runtime
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3000 ENGINE_URL=http://engine:8000
COPY package*.json .npmrc ./
RUN npm ci --omit=dev --ignore-scripts
COPY --chown=node:node --from=build /app/build ./build
COPY --chown=node:node --from=build /app/server.js ./server.js
USER node
EXPOSE 3000
CMD ["node", "server.js"]

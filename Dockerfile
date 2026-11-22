FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS build
WORKDIR /app
COPY package*.json .npmrc ./
RUN npm ci --ignore-scripts
COPY tsconfig.json vite.config.ts server.js ./
COPY static ./static
COPY src ./src
RUN npm run check && npm run build

FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS runtime
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3000 ENGINE_URL=http://engine:8000
COPY package*.json .npmrc ./
RUN npm ci --omit=dev --ignore-scripts
COPY --chown=node:node --from=build /app/build ./build
COPY --chown=node:node --from=build /app/server.js ./server.js
USER node
EXPOSE 3000
CMD ["node", "server.js"]

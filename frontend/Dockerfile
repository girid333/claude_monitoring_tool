# D-022: containerized frontend, multi-stage — mirrors v1's proven pattern
# (sap_monitoring/frontend/Dockerfile) with one deliberate difference: `npm
# install` instead of `npm ci`, because no package-lock.json is committed
# yet (this app has never been through a local `npm install` in this repo).
# `npm ci` requires a lockfile to be present in the build context and will
# fail without one. Run `npm install` once locally and commit the resulting
# package-lock.json to switch this back to `npm ci` for reproducible builds
# (see DEPLOY.md).

FROM node:20-alpine AS base

FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app
COPY package.json ./
RUN npm install

FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_API is baked into the client bundle at build time (Next.js
# convention for NEXT_PUBLIC_* vars) — pass it as a build arg so the built
# image points at the right backend URL for wherever it's deployed.
ARG NEXT_PUBLIC_API=http://localhost:8000
ENV NEXT_PUBLIC_API=$NEXT_PUBLIC_API
RUN npm run build

FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

RUN mkdir .next && chown nextjs:nodejs .next

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

CMD ["node", "server.js"]

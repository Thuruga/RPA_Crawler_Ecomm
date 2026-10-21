# Build Stage
FROM node:22-alpine AS build

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# Copy package.json and pnpm-lock.yaml
COPY package.json pnpm-lock.yaml* ./

# Install dependencies
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install

# Copy the rest of the application
COPY . .

# Configuração pública de build; nunca passar segredos como build args (chaves de máquina nunca vão para VITE_*, D-06)
ARG VITE_AUTH_ENVIRONMENT
ARG VITE_CONNECT_PROXY_URL
ARG VITE_BASE_PATH
ARG VITE_CLERK_PUBLISHABLE_KEY
ARG VITE_AUTH_DISABLED
ARG VITE_DISABLE_AUTH

ENV VITE_AUTH_ENVIRONMENT=$VITE_AUTH_ENVIRONMENT
ENV VITE_CONNECT_PROXY_URL=$VITE_CONNECT_PROXY_URL
ENV VITE_BASE_PATH=$VITE_BASE_PATH
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_AUTH_DISABLED=$VITE_AUTH_DISABLED
ENV VITE_DISABLE_AUTH=$VITE_DISABLE_AUTH

# Build the Vite application
RUN pnpm run build

# Production Stage
FROM nginx:alpine

# Copy custom Nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy build output to Nginx HTML folder
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

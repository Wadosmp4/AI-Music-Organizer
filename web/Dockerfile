FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm install

COPY . .

EXPOSE 5173

# Dev-mode hot reload — U6's Approach note: reaches the backend by container
# DNS name (VITE_API_BASE_URL, set in docker-compose.yml), never localhost.
CMD ["npm", "run", "dev"]

FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --legacy-peer-deps 2>&1 && npm list recharts

COPY . .

EXPOSE 3000
CMD ["npm", "run", "dev"]

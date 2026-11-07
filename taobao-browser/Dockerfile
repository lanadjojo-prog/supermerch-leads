FROM mcr.microsoft.com/playwright:v1.64.1-noble
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY server.mjs ./
ENV NODE_ENV=production
EXPOSE 10000
CMD ["node","server.mjs"]

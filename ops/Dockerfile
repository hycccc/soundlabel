FROM node:20-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY src ./src
COPY prompts ./prompts
COPY persona ./persona
ENV PORT=8787 BIND=0.0.0.0
EXPOSE 8787
CMD ["node", "src/server.mjs"]

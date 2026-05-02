FROM node:22-alpine

WORKDIR /app
COPY cloud_poc/frontend/package.json /app/package.json
RUN npm install
COPY cloud_poc/frontend /app
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

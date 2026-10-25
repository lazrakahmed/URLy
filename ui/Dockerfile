FROM nginxinc/nginx-unprivileged:1.30-alpine
# Pick up OS security fixes published after the base image was built (Trivy gate in CI).
USER root
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js style.css /usr/share/nginx/html/
EXPOSE 8080

# Keep the same major version as the db image in compose.yml.
FROM postgres:18-alpine

RUN apk add --no-cache tzdata rclone age

COPY backup.sh restore.sh row-counts.sh run-backups.sh verify-restore.sh /opt/financial-planner/
RUN chmod 755 /opt/financial-planner/*.sh && mkdir -p /config

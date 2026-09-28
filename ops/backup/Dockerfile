FROM postgres:16-alpine

RUN apk add --no-cache tzdata

COPY backup.sh restore.sh run-backups.sh /opt/financial-planner/
RUN chmod 755 /opt/financial-planner/*.sh

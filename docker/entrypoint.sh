#!/bin/sh
# Legt beim ersten Start die Standardkonfiguration im Datenvolumen ab (vorhandene Dateien bleiben).
set -eu
echo "shotlake: Entrypoint gestartet (Benutzer $(id -u), Datenverzeichnis /data)"
mkdir -p /data/config
for source in /app/config/*; do
  target="/data/config/$(basename "$source")"
  [ -e "$target" ] || cp "$source" "$target"
done
echo "shotlake: Konfiguration bereit, starte: $*"
exec "$@"

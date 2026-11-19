#!/bin/sh
# Legt beim ersten Start die Standardkonfiguration im Datenvolumen ab (vorhandene Dateien bleiben).
set -eu
mkdir -p /data/config
for source in /app/config/*; do
  target="/data/config/$(basename "$source")"
  [ -e "$target" ] || cp "$source" "$target"
done
exec "$@"

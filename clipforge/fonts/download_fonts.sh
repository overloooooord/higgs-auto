#!/usr/bin/env bash
# Качает Montserrat, Inter и Roboto (все с кириллицей) в эту папку.
set -e
cd "$(dirname "$0")"
base="https://github.com/google/fonts/raw/main"
curl -L -o Montserrat-Bold.ttf     "$base/ofl/montserrat/Montserrat%5Bwght%5D.ttf"      || true
curl -L -o Inter-Bold.ttf          "$base/ofl/inter/Inter%5Bopsz,wght%5D.ttf"           || true
curl -L -o Roboto-Bold.ttf         "$base/ofl/roboto/Roboto%5Bwdth,wght%5D.ttf"         || true
echo "Готово. Файлы в $(pwd)"

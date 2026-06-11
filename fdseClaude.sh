#!/usr/bin/env bash
# macOS / Linux 启动包装：
#   chmod +x fdseClaude.sh && ln -s "$(pwd)/fdseClaude.sh" /usr/local/bin/fdseClaude
exec python3 "$(cd "$(dirname "$0")" && pwd)/fdseClaude.py" "$@"

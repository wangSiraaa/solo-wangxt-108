#!/usr/bin/env bash
# 启动用户态 PostgreSQL（无需 root）。数据目录 /workspace/pgdata，端口 5433。
set -euo pipefail
source /workspace/miniforge3/etc/profile.d/conda.sh
conda activate roast
export LD_LIBRARY_PATH=/workspace/miniforge3/envs/roast/lib:${LD_LIBRARY_PATH:-}

PGDATA=/workspace/pgdata
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  initdb -D "$PGDATA" -U roast --auth=trust --no-locale --encoding=UTF8
  {
    echo "port = 5433"
    echo "listen_addresses = 'localhost'"
    echo "unix_socket_directories = '/tmp'"
  } >> "$PGDATA/postgresql.conf"
fi

pg_ctl -D "$PGDATA" -l /workspace/pg.log start
sleep 1
psql -h /tmp -p 5433 -U roast -tAc "SELECT 1 FROM pg_database WHERE datname='roastdb'" | grep -q 1 \
  || createdb -h /tmp -p 5433 -U roast roastdb
echo "PostgreSQL ready on /tmp:5433 (db roastdb)"

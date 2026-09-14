"""PostgreSQL 访问层（psycopg3）。

存储三类原始/人工数据：
- batches          批次元数据
- samples          原始采样：每个期望采样槽一行，探针失联时温度写 NULL（缺测被显式保留）
- events / event_revisions  下豆点、回温点、一爆等热事件 + 风门/燃气操作事件，
                             人工修正保留全部历史版本与来源

本系统不连接真实烘焙机，数据全部来自合成生成器与人工标记。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    variety         TEXT,
    recipe          TEXT,                                 -- 配方标识：同配方不同锅量才有对齐比较意义
    charge_g        DOUBLE PRECISION NOT NULL,          -- 投豆量 g（元数据，不参与曲线计算）
    probe_position  TEXT NOT NULL DEFAULT 'bean-bulk',    -- 探针安装位置（不同位置的温度水平不可直接比）
    probe_offset_c  DOUBLE PRECISION NOT NULL DEFAULT 0,  -- 已知系统性读数偏差（仅元数据，不改原始值）
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 旧库补列（幂等）
ALTER TABLE batches ADD COLUMN IF NOT EXISTS recipe TEXT;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS probe_position TEXT NOT NULL DEFAULT 'bean-bulk';
ALTER TABLE batches ADD COLUMN IF NOT EXISTS probe_offset_c DOUBLE PRECISION NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS samples (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    t_s             DOUBLE PRECISION NOT NULL,          -- 相对下豆点的秒数（可非等间隔）
    bean_temp_c     DOUBLE PRECISION,                   -- 实测豆温；探针失联为 NULL
    env_temp_c      DOUBLE PRECISION,                   -- 实测环境温度；NULL 表示缺测
    is_missing      BOOLEAN NOT NULL DEFAULT FALSE,     -- TRUE = 该采样槽探针失联/未采到
    UNIQUE (batch_id, t_s)
);
CREATE INDEX IF NOT EXISTS idx_samples_batch_t ON samples(batch_id, t_s);

-- 热事件：charge(下豆/投入) / turnaround(回温点) / yellow(变黄) /
--         first_crack(一爆) / drop(出豆)
-- 操作事件：damper(风门 0-100) / gas(燃气 0-100)
CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN
                        ('charge','turnaround','yellow','first_crack','drop',
                         'damper','gas')),
    t_s             DOUBLE PRECISION NOT NULL,
    value           DOUBLE PRECISION,                   -- 操作事件的档位；热事件为 NULL
    source          TEXT NOT NULL DEFAULT 'auto'
                        CHECK (source IN ('auto','manual')),
    note            TEXT,
    created_by      TEXT NOT NULL DEFAULT 'operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,                       -- 被人工修正后作废旧版本
    UNIQUE (batch_id, kind, t_s)
);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, kind)
    WHERE revoked_at IS NULL;

-- 人工修正的完整审计轨迹：对同批次同 kind，保留所有版本及来源
CREATE TABLE IF NOT EXISTS event_revisions (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    event_id        BIGINT REFERENCES events(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    old_t_s         DOUBLE PRECISION,
    old_source      TEXT,
    new_t_s         DOUBLE PRECISION NOT NULL,
    new_value       DOUBLE PRECISION,
    new_source      TEXT NOT NULL,
    reason          TEXT,
    revised_by      TEXT NOT NULL DEFAULT 'operator',
    revised_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 候选修订：负责人对疑似误标建立的“建议”，不改变当前有效事件，需确认才生效
CREATE TABLE IF NOT EXISTS event_candidates (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    proposed_t_s    DOUBLE PRECISION NOT NULL,
    proposed_value  DOUBLE PRECISION,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','accepted','rejected')),
    proposed_by     TEXT NOT NULL DEFAULT 'operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ
);

-- 比较报告：创建时快照两批次各自的“事件版本集”，旧报告永久绑定旧事件版本，
-- 之后事件再被修正也不改变已存报告的结论。
CREATE TABLE IF NOT EXISTS comparison_reports (
    id              BIGSERIAL PRIMARY KEY,
    batch_a_id      BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    batch_b_id      BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    alignment       TEXT NOT NULL DEFAULT 'physical'
                        CHECK (alignment IN ('physical','phase')),
    smooth_window_s DOUBLE PRECISION NOT NULL,
    ror_window_s    DOUBLE PRECISION NOT NULL,
    -- 快照：事件版本（含来源）、指标、对齐/可比性信息、派生曲线（可直接重放）
    snapshot        JSONB NOT NULL,
    event_version_a JSONB NOT NULL,
    event_version_b JSONB NOT NULL,
    note            TEXT,
    created_by      TEXT NOT NULL DEFAULT 'operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 对比结论备注与待办：绑定创建时的批次对、对齐模式、计算参数与事件版本快照，
-- 后续事件修正不改变既有备注所依据的快照。
CREATE TABLE IF NOT EXISTS comparison_notes (
    id              BIGSERIAL PRIMARY KEY,
    batch_a_id      BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    batch_b_id      BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    alignment       TEXT NOT NULL CHECK (alignment IN ('physical','phase')),
    smooth_window_s DOUBLE PRECISION NOT NULL,
    ror_window_s    DOUBLE PRECISION NOT NULL,
    conclusion      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'followup'
                        CHECK (status IN ('followup','confirmed','discarded')),
    owner           TEXT,
    due_date        DATE,
    event_version_a JSONB NOT NULL,   -- 创建时批次A有效事件快照
    event_version_b JSONB NOT NULL,   -- 创建时批次B有效事件快照
    anchor_snapshot JSONB,            -- 创建时锚点/可比性摘要（便于回溯结论依据）
    created_by      TEXT NOT NULL DEFAULT 'operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notes_pair ON comparison_notes(batch_a_id, batch_b_id);

-- 备注状态流转的审计轨迹：每次状态变更保留旧/新状态、原因、操作人、时间
CREATE TABLE IF NOT EXISTS note_status_revisions (
    id              BIGSERIAL PRIMARY KEY,
    note_id         BIGINT NOT NULL REFERENCES comparison_notes(id) ON DELETE CASCADE,
    old_status      TEXT,             -- NULL 表示创建时的初始状态
    new_status      TEXT NOT NULL CHECK (new_status IN ('followup','confirmed','discarded')),
    reason          TEXT,
    changed_by      TEXT NOT NULL DEFAULT 'operator',
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_note_rev_note ON note_status_revisions(note_id);

-- 阶段复盘标签：绑定到“创建时那个具体事件版本”，事件后续修正时旧标签仍保留原快照。
CREATE TABLE IF NOT EXISTS event_tags (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    event_kind      TEXT NOT NULL CHECK
                        (event_kind IN ('charge','turnaround','yellow','first_crack','drop','damper','gas')),
    label           TEXT NOT NULL,
    description     TEXT,
    -- 处理闭环状态
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','adopted','ignored')),
    resolution      TEXT,                    -- 最近一次处理结论
    resolved_by     TEXT,                    -- 处理人
    resolved_at     TIMESTAMPTZ,
    -- 创建时的事件版本快照（处理流转永不修改这部分）
    bound_event_id  BIGINT REFERENCES events(id) ON DELETE SET NULL,
    event_t_s       DOUBLE PRECISION NOT NULL,   -- 标签创建时该事件的时间
    event_source    TEXT NOT NULL,              -- 标签创建时该事件的来源(auto/manual)
    created_by      TEXT NOT NULL DEFAULT 'operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tags_batch ON event_tags(batch_id, event_kind);

-- 标签处理历史：只追加。删除标签时随之一并删除，不触碰事件/事件修订。
CREATE TABLE IF NOT EXISTS tag_status_revisions (
    id              BIGSERIAL PRIMARY KEY,
    tag_id          BIGINT NOT NULL REFERENCES event_tags(id) ON DELETE CASCADE,
    old_status      TEXT,                  -- NULL = 创建时的初始状态
    new_status      TEXT NOT NULL CHECK (new_status IN ('open','adopted','ignored')),
    resolution      TEXT,
    changed_by      TEXT NOT NULL DEFAULT 'operator',
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tag_rev_tag ON tag_status_revisions(tag_id);

-- 旧库补列（幂等）
ALTER TABLE event_tags ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';
ALTER TABLE event_tags ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE event_tags ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE event_tags ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
"""


def init_schema() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        conn.commit()


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.pg_dsn, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

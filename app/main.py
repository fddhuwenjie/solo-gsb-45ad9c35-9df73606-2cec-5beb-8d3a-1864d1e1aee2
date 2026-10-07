"""双语文稿定版 HTTP API。

核心不变量：
  * 工作稿修订号 revision 只在原文真正改变时 +1（全文共享一个号）：
    原文按段改写 +1；段落编排（重排 / 删除 / 新增）整次提交也只 +1。
  * 译文必须注明所依据的原文版本；依据过期版本的提交 / 确认一律 409 拒绝。
  * 原文改版只影响对应段：该段已确认译文翻转为 stale（待复核），其他段不动。
  * 段落编排一次提交原子调整整篇顺序：成功后位置从 0 连续重排；保留段携带
    自己的原文版本、译文与确认状态移动；未列出的旧段连同其历史一并删除；
    新段从原文版本 1 开始且尚无译文。修订号过期或清单无效时整次拒绝，不留
    部分改动。
  * 发布必须携带 expected_revision，且每段都有匹配"当前原文版本"的
    已确认译文，否则要么 409 修订冲突，要么 422 返回阻塞段，且不增加版号。
  * 定版以整篇 JSON 快照落库，按版号读取永不随后续修改 / 复核 / 编排变化；
    编排与发布通过写锁互斥，定版只能定格调整前或调整后的完整一整篇。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    ConfirmTranslation,
    CreateManuscript,
    DraftView,
    PublicationView,
    PublishRequest,
    ReorderSegments,
    RewriteSource,
    SEGMENT_MISSING,
    SEGMENT_STALE,
    SubmitTranslation,
)
from .db import (
    STATUS_CONFIRMED,
    STATUS_STALE,
    STATUS_SUBMITTED,
)


# ---- 辅助 ----------------------------------------------------------------


def _get_manuscript(conn: sqlite3.Connection, manuscript_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, title, revision FROM manuscripts WHERE id = ?",
        (manuscript_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "稿件不存在")
    return row


def _get_segment(conn: sqlite3.Connection, manuscript_id: int, position: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, position, source_version, source_text "
        "FROM segments WHERE manuscript_id = ? AND position = ?",
        (manuscript_id, position),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "原文段不存在")
    return row


def _conflict(detail: str) -> HTTPException:
    """依据过期版本 / 修订号不匹配：调用方看到的状态已过期。"""
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def _translation_view(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "source_version": row["source_version"],
        "target_text": row["target_text"],
        "status": row["status"],
        "updated_at": row["updated_at"],
    }


def _build_draft(conn: sqlite3.Connection, m: sqlite3.Row) -> dict:
    """组装工作稿：各段当前原文版本、译文与段状态。"""
    segments = conn.execute(
        "SELECT id, position, source_version, source_text, updated_at "
        "FROM segments WHERE manuscript_id = ? ORDER BY position",
        (m["id"],),
    ).fetchall()

    seg_views: list[dict] = []
    for seg in segments:
        # 当前版本对应的活跃译文（submitted / confirmed 至多一条）
        active = conn.execute(
            "SELECT source_version, target_text, status, updated_at "
            "FROM translations "
            "WHERE segment_id = ? AND source_version = ? AND status != 'stale'",
            (seg["id"], seg["source_version"]),
        ).fetchone()

        # 最近一条失效待复核的旧译文（旧版本），供复核参考
        stale = conn.execute(
            "SELECT source_version, target_text, status, updated_at "
            "FROM translations WHERE segment_id = ? AND status = 'stale' "
            "ORDER BY source_version DESC, updated_at DESC LIMIT 1",
            (seg["id"],),
        ).fetchone()

        if active is not None:
            seg_status = active["status"]  # submitted / confirmed
        elif stale is not None:
            seg_status = SEGMENT_STALE
        else:
            seg_status = SEGMENT_MISSING

        seg_views.append(
            {
                "position": seg["position"],
                "source_text": seg["source_text"],
                "source_version": seg["source_version"],
                "status": seg_status,
                "translation": _translation_view(active),
                "stale_translation": _translation_view(stale),
            }
        )

    return {
        "manuscript_id": m["id"],
        "title": m["title"],
        "revision": m["revision"],
        "segments": seg_views,
    }


# ---- 应用 ----------------------------------------------------------------


class UTF8JSONResponse(JSONResponse):
    """双语内容直接输出 UTF-8 中文，而不是转义成 \\uXXXX。"""

    def render(self, content: object) -> bytes:
        return json.dumps(
            content, ensure_ascii=False, allow_nan=False,
            indent=None, separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    path = db.db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    db.init_db(path)
    yield


app = FastAPI(
    title="双语文稿定版 API",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError) -> UTF8JSONResponse:
    return UTF8JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(exc.errors())},
    )


# ---- 稿件 ----------------------------------------------------------------


@app.post("/manuscripts", status_code=status.HTTP_201_CREATED)
def create_manuscript(
    payload: CreateManuscript,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """创建稿件并录入有序原文段。段内不允许空字符串。"""
    for i, text in enumerate(payload.segments):
        if not text.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"第 {i} 段原文为空",
            )

    with db.write_tx(conn):
        cur = conn.execute(
            "INSERT INTO manuscripts (title) VALUES (?)", (payload.title,)
        )
        manuscript_id = cur.lastrowid
        for position, text in enumerate(payload.segments):
            seg_cur = conn.execute(
                "INSERT INTO segments (manuscript_id, position, source_version, source_text) "
                "VALUES (?, ?, 1, ?)",
                (manuscript_id, position, text),
            )
            conn.execute(
                "INSERT INTO source_revisions (segment_id, version, source_text) "
                "VALUES (?, 1, ?)",
                (seg_cur.lastrowid, text),
            )

    return {"manuscript_id": manuscript_id, "revision": 1, "segment_count": len(payload.segments)}


@app.get("/manuscripts/{manuscript_id}")
def get_manuscript(
    manuscript_id: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    m = _get_manuscript(conn, manuscript_id)
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM segments WHERE manuscript_id = ?",
        (manuscript_id,),
    ).fetchone()["c"]
    return {"manuscript_id": m["id"], "title": m["title"], "revision": m["revision"], "segment_count": count}


@app.get("/manuscripts/{manuscript_id}/draft", response_model=DraftView)
def get_draft(
    manuscript_id: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """查询工作稿：显示每段的原文版本、译文与状态。"""
    m = _get_manuscript(conn, manuscript_id)
    return _build_draft(conn, m)


# ---- 原文改写 -------------------------------------------------------------


@app.put("/manuscripts/{manuscript_id}/segments/{position}/source")
def rewrite_source(
    manuscript_id: int,
    position: int,
    payload: RewriteSource,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """改写某段原文。

    携带 expected_revision 做乐观并发控制：与当前修订号不一致即 409。
    内容真正变化时：段版本 +1、工作稿修订号 +1，且该段已确认译文失效
    （置为 stale，待复核），其他段不受影响。内容未变化则幂等返回。
    """
    with db.write_tx(conn):
        m = _get_manuscript(conn, manuscript_id)
        if m["revision"] != payload.expected_revision:
            raise _conflict(
                f"修订冲突：工作稿当前修订号为 {m['revision']}，"
                f"请求依据的是 {payload.expected_revision}"
            )
        seg = _get_segment(conn, manuscript_id, position)

        if not payload.text.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "原文内容不能为空")

        if seg["source_text"] == payload.text:
            return {
                "manuscript_id": manuscript_id,
                "position": position,
                "source_version": seg["source_version"],
                "revision": m["revision"],
                "changed": False,
            }

        new_version = seg["source_version"] + 1
        conn.execute(
            "UPDATE segments SET source_version = ?, source_text = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (new_version, payload.text, seg["id"]),
        )
        conn.execute(
            "INSERT INTO source_revisions (segment_id, version, source_text) VALUES (?, ?, ?)",
            (seg["id"], new_version, payload.text),
        )
        # 只让该段"已确认"译文失效；依据旧版本尚未确认的提交保持原样，
        # 但它们同样依据旧版本，段状态会体现为待复核 / 待提交。
        conn.execute(
            "UPDATE translations SET status = ?, updated_at = datetime('now') "
            "WHERE segment_id = ? AND status = ?",
            (STATUS_STALE, seg["id"], STATUS_CONFIRMED),
        )
        conn.execute(
            "UPDATE manuscripts SET revision = revision + 1 WHERE id = ?",
            (manuscript_id,),
        )
        new_revision = m["revision"] + 1

    return {
        "manuscript_id": manuscript_id,
        "position": position,
        "source_version": new_version,
        "revision": new_revision,
        "changed": True,
    }


# ---- 段落编排 -------------------------------------------------------------


@app.post("/manuscripts/{manuscript_id}/reorder")
def reorder_segments(
    manuscript_id: int,
    payload: ReorderSegments,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """一次提交重排整篇工作稿的段落顺序（原子操作）。

    清单项二选一：
      * {"source_position": k} —— 引用调整前位置 k 的旧段（不可重复引用）；
      * {"source_text": "..."} —— 在此插入一段新原文（版本 1，尚无译文）。

    成功后：
      * 新位置从 0 连续编号；保留段的原文版本、译文与确认状态随段移动；
      * 未被列出的旧段（连同其译文与原文版本历史）从工作稿移除；
      * 新段从原文版本 1 开始，状态 missing；
      * 工作稿修订号只增加一次。

    expected_revision 过期 -> 409；清单无效（旧段越界 / 重复引用 / 新段为
    空白文本 / 一个旧段都没保留）-> 422。任何失败都在同一立即写事务内
    回滚，不留部分改动；与发布互斥，定版不会跨编排拼装。
    """
    with db.write_tx(conn):
        m = _get_manuscript(conn, manuscript_id)
        if m["revision"] != payload.expected_revision:
            raise _conflict(
                f"修订冲突：工作稿当前修订号为 {m['revision']}，"
                f"编排请求依据的是 {payload.expected_revision}"
            )

        old_segments = conn.execute(
            "SELECT id, position, source_version, source_text "
            "FROM segments WHERE manuscript_id = ? ORDER BY position",
            (manuscript_id,),
        ).fetchall()
        old_by_position = {seg["position"]: seg for seg in old_segments}

        referenced: set[int] = set()
        for index, item in enumerate(payload.segments):
            if item.source_position is not None:
                old_pos = item.source_position
                if old_pos not in old_by_position:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "invalid_reorder",
                            "message": f"清单项 {index} 引用的调整前位置 {old_pos} 不存在",
                            "index": index,
                        },
                    )
                if old_pos in referenced:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "invalid_reorder",
                            "message": f"调整前位置 {old_pos} 被重复引用",
                            "index": index,
                        },
                    )
                referenced.add(old_pos)
            else:
                # 字段层 min_length=1 只挡空串，纯空白在此补一道校验
                assert item.source_text is not None
                if not item.source_text.strip():
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "invalid_reorder",
                            "message": f"清单项 {index} 的新原文为空白",
                            "index": index,
                        },
                    )

        # 1) 删除未列出的旧段：先删译文与原文版本历史，再删段本身。
        removed = [seg for pos, seg in old_by_position.items() if pos not in referenced]
        if removed:
            removed_ids = [seg["id"] for seg in removed]
            placeholders = ",".join("?" for _ in removed_ids)
            conn.execute(
                f"DELETE FROM translations WHERE segment_id IN ({placeholders})",
                removed_ids,
            )
            conn.execute(
                f"DELETE FROM source_revisions WHERE segment_id IN ({placeholders})",
                removed_ids,
            )
            conn.execute(
                f"DELETE FROM segments WHERE id IN ({placeholders})",
                removed_ids,
            )

        # 2) 保留段移动到新位置（其原文版本、译文、确认状态原样跟随）。
        for new_position, item in enumerate(payload.segments):
            if item.source_position is not None:
                seg = old_by_position[item.source_position]
                conn.execute(
                    "UPDATE segments SET position = ? WHERE id = ?",
                    (new_position, seg["id"]),
                )

        # 3) 插入新段：当前原文版本 1，同时写入版本历史；无任何译文。
        for new_position, item in enumerate(payload.segments):
            if item.source_text is not None:
                seg_cur = conn.execute(
                    "INSERT INTO segments (manuscript_id, position, source_version, source_text) "
                    "VALUES (?, ?, 1, ?)",
                    (manuscript_id, new_position, item.source_text),
                )
                conn.execute(
                    "INSERT INTO source_revisions (segment_id, version, source_text) "
                    "VALUES (?, 1, ?)",
                    (seg_cur.lastrowid, item.source_text),
                )

        # 整次编排只推进一次工作稿修订号。
        conn.execute(
            "UPDATE manuscripts SET revision = revision + 1 WHERE id = ?",
            (manuscript_id,),
        )
        new_revision = m["revision"] + 1

    return {
        "manuscript_id": manuscript_id,
        "revision": new_revision,
        "segment_count": len(payload.segments),
    }


# ---- 译文提交 / 确认 -------------------------------------------------------


@app.put("/manuscripts/{manuscript_id}/segments/{position}/translation")
def submit_translation(
    manuscript_id: int,
    position: int,
    payload: SubmitTranslation,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """提交（或改写）某段译文，必须注明所依据的原文版本。

    依据过期原文版本一律 409 拒绝。重复提交同一版本视为改写译文，
    状态回到 submitted，需要重新确认。
    """
    with db.write_tx(conn):
        _get_manuscript(conn, manuscript_id)
        seg = _get_segment(conn, manuscript_id, position)
        if not payload.text.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "译文内容不能为空")
        if payload.source_version != seg["source_version"]:
            raise _conflict(
                f"原文版本过期：该段当前版本为 {seg['source_version']}，"
                f"译文依据的是 {payload.source_version}"
            )

        existing = conn.execute(
            "SELECT id FROM translations WHERE segment_id = ? AND source_version = ? "
            "AND status != 'stale'",
            (seg["id"], payload.source_version),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO translations (segment_id, source_version, target_text, status) "
                "VALUES (?, ?, ?, ?)",
                (seg["id"], payload.source_version, payload.text, STATUS_SUBMITTED),
            )
        else:
            conn.execute(
                "UPDATE translations SET target_text = ?, status = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (payload.text, STATUS_SUBMITTED, existing["id"]),
            )

    return {
        "manuscript_id": manuscript_id,
        "position": position,
        "source_version": payload.source_version,
        "status": STATUS_SUBMITTED,
    }


@app.post("/manuscripts/{manuscript_id}/segments/{position}/confirmation")
def confirm_translation(
    manuscript_id: int,
    position: int,
    payload: ConfirmTranslation,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """确认译文。只能确认依据"当前原文版本"且已提交的译文。

    依据过期版本、或该版本尚无译文，都返回 409（确认依据过期，拒绝）。
    对已是 confirmed 的同版本确认是幂等的。
    """
    with db.write_tx(conn):
        _get_manuscript(conn, manuscript_id)
        seg = _get_segment(conn, manuscript_id, position)
        if payload.source_version != seg["source_version"]:
            raise _conflict(
                f"原文版本过期：该段当前版本为 {seg['source_version']}，"
                f"确认依据的是 {payload.source_version}"
            )
        row = conn.execute(
            "SELECT id, status FROM translations "
            "WHERE segment_id = ? AND source_version = ? AND status != 'stale'",
            (seg["id"], payload.source_version),
        ).fetchone()
        if row is None:
            raise _conflict(
                f"该段原文版本 {payload.source_version} 尚无译文提交，无法确认"
            )
        if row["status"] != STATUS_CONFIRMED:
            conn.execute(
                "UPDATE translations SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (STATUS_CONFIRMED, row["id"]),
            )

    return {
        "manuscript_id": manuscript_id,
        "position": position,
        "source_version": payload.source_version,
        "status": STATUS_CONFIRMED,
    }


# ---- 发布定版 -------------------------------------------------------------


@app.post("/manuscripts/{manuscript_id}/publications", response_model=PublicationView, status_code=201)
def publish(
    manuscript_id: int,
    payload: PublishRequest,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """原子发布定版。

    1. expected_revision 与当前工作稿修订号不一致 -> 409，不增加版号。
    2. 任一段缺少匹配当前原文版本的已确认译文 -> 422，body 中列出阻塞段。
    3. 全部满足 -> 在同一写事务内取下一版号并写入整篇快照。
    事务由 BEGIN IMMEDIATE 起步，与原文改写互斥，快照不可能混入不同
    工作稿修订的数据。
    """
    with db.write_tx(conn):
        m = _get_manuscript(conn, manuscript_id)
        if m["revision"] != payload.expected_revision:
            raise _conflict(
                f"修订冲突：工作稿当前修订号为 {m['revision']}，"
                f"发布请求依据的是 {payload.expected_revision}"
            )

        segments = conn.execute(
            "SELECT id, position, source_version, source_text "
            "FROM segments WHERE manuscript_id = ? ORDER BY position",
            (manuscript_id,),
        ).fetchall()

        blocking: list[dict] = []
        paired: list[dict] = []
        for seg in segments:
            tr = conn.execute(
                "SELECT target_text FROM translations "
                "WHERE segment_id = ? AND source_version = ? AND status = ?",
                (seg["id"], seg["source_version"], STATUS_CONFIRMED),
            ).fetchone()
            if tr is None:
                blocking.append({"position": seg["position"], "source_version": seg["source_version"]})
            else:
                paired.append(
                    {
                        "position": seg["position"],
                        "source_version": seg["source_version"],
                        "source_text": seg["source_text"],
                        "target_text": tr["target_text"],
                    }
                )

        if blocking:
            # 422 必须在事务内抛出：write_tx 会回滚，版号不增加。
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "segments_not_confirmed", "blocking_segments": blocking},
            )

        edition_row = conn.execute(
            "SELECT COALESCE(MAX(edition), 0) + 1 AS next_edition "
            "FROM publications WHERE manuscript_id = ?",
            (manuscript_id,),
        ).fetchone()
        edition = edition_row["next_edition"]
        snapshot = {
            "edition": edition,
            "revision": m["revision"],
            "title": m["title"],
            "segments": paired,
        }
        conn.execute(
            "INSERT INTO publications (manuscript_id, edition, revision, title, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (manuscript_id, edition, m["revision"], m["title"], json.dumps(snapshot, ensure_ascii=False)),
        )
        published_at = conn.execute(
            "SELECT created_at FROM publications WHERE manuscript_id = ? AND edition = ?",
            (manuscript_id, edition),
        ).fetchone()["created_at"]

    result = dict(snapshot)
    result["published_at"] = published_at
    return result


@app.get("/manuscripts/{manuscript_id}/publications", response_model=list[PublicationView])
def list_publications(
    manuscript_id: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> list[dict]:
    _get_manuscript(conn, manuscript_id)
    rows = conn.execute(
        "SELECT edition, revision, title, snapshot_json, created_at "
        "FROM publications WHERE manuscript_id = ? ORDER BY edition",
        (manuscript_id,),
    ).fetchall()
    return [_publication_view(r) for r in rows]


@app.get("/manuscripts/{manuscript_id}/publications/{edition}", response_model=PublicationView)
def get_publication(
    manuscript_id: int,
    edition: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """按版号读取定版：内容直接来自发布时落库的不可变快照。"""
    _get_manuscript(conn, manuscript_id)
    row = conn.execute(
        "SELECT edition, revision, title, snapshot_json, created_at "
        "FROM publications WHERE manuscript_id = ? AND edition = ?",
        (manuscript_id, edition),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定版不存在")
    return _publication_view(row)


def _publication_view(row: sqlite3.Row) -> dict:
    snapshot = json.loads(row["snapshot_json"])
    return {
        "edition": row["edition"],
        "revision": row["revision"],
        "title": row["title"],
        "published_at": row["created_at"],
        "segments": snapshot["segments"],
    }

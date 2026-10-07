"""端到端行为测试，使用 TestClient + 临时 SQLite 文件库。

覆盖：
  * 创建、工作稿各段版本 / 状态流转
  * 依据过期版本的提交与确认被拒绝
  * 原文改版仅令该段已确认译文失效（stale），他段不受影响
  * 发布的修订冲突、阻塞段返回、失败不增加版号
  * 定版不可变：后续改写 / 复核后旧版号读取内容不变
  * 发布与原文改写并发：每次成功发布都与某个完整工作稿修订严格一致
  * 读者生效版指针：尚无定版显式 404；发布原子推进；失败不回退
  * 生效版切换的 CAS 语义、切换不动工作稿 / 不增版号 / 快照不变
  * 切换与发布交错：过期切换绝不覆盖其间新发布的定版；读者读到的
    永远是一份完整发布的快照
"""

from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    # 确保 app 模块在设置环境变量之后读取 DB_PATH
    import importlib

    import app.db as db_mod
    import app.main as main_mod

    importlib.reload(db_mod)
    importlib.reload(main_mod)
    db_mod.init_db()
    with TestClient(main_mod.app) as c:
        yield c


def _create(client, n=2, title="示例"):
    r = client.post(
        "/manuscripts",
        json={"title": title, "segments": [f"原文{i}" for i in range(n)]},
    )
    assert r.status_code == 201, r.text
    return r.json()["manuscript_id"]


def _submit(client, mid, pos, text, ver):
    return client.put(
        f"/manuscripts/{mid}/segments/{pos}/translation",
        json={"text": text, "source_version": ver},
    )


def _confirm(client, mid, pos, ver):
    return client.post(
        f"/manuscripts/{mid}/segments/{pos}/confirmation",
        json={"source_version": ver},
    )


def _publish(client, mid, rev):
    return client.post(
        f"/manuscripts/{mid}/publications", json={"expected_revision": rev}
    )


def _reorder(client, mid, expected_revision, items):
    return client.post(
        f"/manuscripts/{mid}/reorder",
        json={"expected_revision": expected_revision, "segments": items},
    )


def _ref(pos):
    return {"source_position": pos}


def _new(text):
    return {"source_text": text}


# ---- 基本流程 -------------------------------------------------------------


def test_create_and_draft_states(client):
    mid = _create(client, 2)
    r = client.get(f"/manuscripts/{mid}/draft")
    assert r.status_code == 200
    draft = r.json()
    assert draft["revision"] == 1
    assert [s["position"] for s in draft["segments"]] == [0, 1]
    assert all(s["source_version"] == 1 for s in draft["segments"])
    assert all(s["status"] == "missing" for s in draft["segments"])


def test_submit_confirm_flow(client):
    mid = _create(client)
    assert _submit(client, mid, 0, "trans-0", 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "submitted"
    assert draft["segments"][1]["status"] == "missing"

    assert _confirm(client, mid, 0, 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "confirmed"
    assert draft["segments"][0]["translation"]["target_text"] == "trans-0"

    # 再次提交同一版本：译文被改写，确认状态回到 submitted
    assert _submit(client, mid, 0, "trans-0-new", 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "submitted"
    assert draft["segments"][0]["translation"]["target_text"] == "trans-0-new"


# ---- 过期版本 -------------------------------------------------------------


def test_submit_and_confirm_against_stale_version_rejected(client):
    mid = _create(client)
    _submit(client, mid, 0, "old", 1)
    _confirm(client, mid, 0, 1)

    # 原文改版 -> 修订号与段版本各 +1
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-新版", "expected_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["source_version"] == 2
    assert r.json()["revision"] == 2

    # 依据旧版本提交 / 确认都被拒绝
    assert _submit(client, mid, 0, "x", 1).status_code == 409
    assert _confirm(client, mid, 0, 1).status_code == 409
    # 依据不存在的未来版本同样拒绝
    assert _submit(client, mid, 0, "x", 3).status_code == 409


def test_rewrite_only_invalidates_that_segment(client):
    mid = _create(client, 3)
    for i in range(3):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    r = client.put(
        f"/manuscripts/{mid}/segments/1/source",
        json={"text": "原文1-改", "expected_revision": 1},
    )
    assert r.status_code == 200

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 2
    statuses = {s["position"]: s for s in draft["segments"]}
    assert statuses[0]["status"] == "confirmed"          # 他段不受影响
    assert statuses[2]["status"] == "confirmed"
    assert statuses[1]["status"] == "stale"              # 该段待复核
    assert statuses[1]["source_version"] == 2
    assert statuses[1]["stale_translation"]["target_text"] == "t1"
    assert statuses[1]["stale_translation"]["status"] == "stale"


def test_rewrite_revision_conflict_and_idempotence(client):
    mid = _create(client)
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "A", "expected_revision": 99},
    )
    assert r.status_code == 409

    # 内容相同：不产生新版本、不加修订号
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0", "expected_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.get(f"/manuscripts/{mid}").json()["revision"] == 1


# ---- 发布 ----------------------------------------------------------------


def test_publish_blocks_and_does_not_bump_edition(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    # 第 1 段只有 submitted，没有 confirmed

    # 修订号不匹配
    assert _publish(client, mid, 5).status_code == 409

    # 匹配但存在阻塞段
    r = _publish(client, mid, 1)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [{"position": 1, "source_version": 1}]

    # 没有产生任何定版
    assert client.get(f"/manuscripts/{mid}/publications").json() == []


def test_publish_success_and_immutable_snapshot(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    _submit(client, mid, 1, "t1", 1)
    _confirm(client, mid, 1, 1)

    r = _publish(client, mid, 1)
    assert r.status_code == 201, r.text
    pub1 = r.json()
    assert pub1["edition"] == 1
    assert pub1["revision"] == 1
    assert [s["target_text"] for s in pub1["segments"]] == ["t0", "t1"]

    # 发布后改写原文、重新翻译确认、再发布第二版
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )
    _submit(client, mid, 0, "t0-v2", 2)
    _confirm(client, mid, 0, 2)
    r = _publish(client, mid, 2)
    assert r.status_code == 201
    assert r.json()["edition"] == 2

    # 旧版号读取结果不受后续修改 / 复核影响
    old = client.get(f"/manuscripts/{mid}/publications/1").json()
    assert old["revision"] == 1
    assert old["segments"][0]["source_text"] == "原文0"
    assert old["segments"][0]["target_text"] == "t0"

    assert client.get(f"/manuscripts/{mid}/publications/9").status_code == 404


def test_revision_moves_under_unconfirmed_publish(client):
    # 发布与改写交错时只有两种合法结局，都不允许混入不同修订的数据：
    #   1) 改写先提交 -> 发布 409；
    #   2) 发布先提交 -> 定版 201（定格 rev1），改写随后仍可基于 rev1 成功
    #      （发布不推进工作稿修订号）。
    mid = _create(client, 1)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)

    barrier = threading.Barrier(2)

    def rewrite():
        barrier.wait()
        r = client.put(
            f"/manuscripts/{mid}/segments/0/source",
            json={"text": "原文0-new", "expected_revision": 1},
        )
        return r.status_code, r.json() if r.status_code == 200 else None

    def publish():
        barrier.wait()
        r = _publish(client, mid, 1)
        return r.status_code, r.json() if r.status_code == 201 else None

    with ThreadPoolExecutor(max_workers=2) as ex:
        rw, pb = list(ex.map(lambda f: f(), [rewrite, publish]))

    rw_code, _ = rw
    pb_code, pb_body = pb
    assert rw_code in (200, 409)
    assert pb_code in (201, 409)
    assert (rw_code, pb_code) in {(200, 409), (200, 201), (409, 201)}

    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    if pb_code == 201:
        # 定版只能定格 rev1 的原文，绝不能出现改写后的文本
        assert pb_body["revision"] == 1
        assert pb_body["segments"][0]["source_text"] == "原文0"
        assert len(pubs) == 1
        assert pubs[0]["segments"][0]["source_text"] == "原文0"
    else:
        assert pubs == []


def test_concurrent_rewrite_and_publish_never_mix_revisions(client):
    """反复并发：成功的定版快照必须与某个真实工作稿修订整体一致。"""
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}-v1", 1)
        _confirm(client, mid, i, 1)

    outcomes = []

    def publish_once(expected_rev):
        r = _publish(client, mid, expected_rev)
        return ("pub", r.status_code, r.json() if r.status_code == 201 else None)

    def rewrite_once(expected_rev, pos, text):
        r = client.put(
            f"/manuscripts/{mid}/segments/{pos}/source",
            json={"text": text, "expected_revision": expected_rev},
        )
        return ("rewrite", r.status_code, r.json() if r.status_code == 200 else None)

    # 场景：rev1 两段齐备；rev2 改第 0 段（第 1 段未重新确认）。
    # rev2 下发布必须 422；成功发布只能定格 rev1 的全文。
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(publish_once, 1),
            ex.submit(rewrite_once, 1, 0, "src0-v2"),
        ]
        for f in futs:
            outcomes.append(f.result())

    for kind, code, body in outcomes:
        if kind == "pub" and code == 201:
            assert body["revision"] == 1
            assert body["segments"][0]["source_text"] == "原文0"
            assert body["segments"][1]["source_text"] == "原文1"

    # 若发布抢在改写前成功，改写后仍可基于 rev2 看到阻塞段
    if any(k == "rewrite" and c == 200 for k, c, _ in outcomes):
        r = _publish(client, mid, 2)
        assert r.status_code == 422
        assert r.json()["detail"]["blocking_segments"] == [{"position": 0, "source_version": 2}]


def test_stress_concurrent_rewrite_publish_snapshots_consistent(client):
    """多轮压测：工作稿 rev N 下只确认第 0 段的新译文（第 1 段保持旧确认），
    则任何成功定版都必须完整对应某一已知修订，绝不允许跨修订拼装。"""
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}-v1", 1)
        _confirm(client, mid, i, 1)

    revision = 1
    version0 = 1
    lock = threading.Lock()

    def do_round(_):
        nonlocal revision, version0
        with lock:
            expected = revision
            version0 += 1
            new_text = f"src0-v{version0}"
            new_target = f"t0-v{version0}"

        rewrite_code = client.put(
            f"/manuscripts/{mid}/segments/0/source",
            json={"text": new_text, "expected_revision": expected},
        ).status_code
        with lock:
            if rewrite_code == 200:
                revision = expected + 1
                current_version0 = version0
            else:
                current_version0 = None

        pub_code = client.post(
            f"/manuscripts/{mid}/publications", json={"expected_revision": expected}
        ).status_code

        # 无论先后，422（段不齐）与 409（修订漂移）都可能出现；
        # 201 仅在 rev 仍为 expected 且两段都匹配当前版本时合法。
        assert pub_code in (201, 409, 422)
        assert rewrite_code in (200, 409)
        if pub_code == 201:
            body = client.get(
                f"/manuscripts/{mid}/publications",
            ).json()
            latest = body[-1]
            assert latest["revision"] == expected
            seg0, seg1 = latest["segments"]
            # 定版瞬间：要么两段都停在 v1，要么两段一致地处于更新后的状态
            assert seg1["source_version"] == 1 and seg1["target_text"] == "t1-v1"
            if rewrite_code == 409:
                assert seg0["source_version"] == 1
            else:
                # 改写已提交，201 只可能发生在"改写之前"，故定格仍是 v1
                assert seg0["source_version"] == 1
        # 改写成功后补确认，让下一轮有更多可发布的交错点
        if current_version0 is not None:
            _submit(client, mid, 0, new_target, current_version0)
            _confirm(client, mid, 0, current_version0)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(do_round, range(15)))

    # 最终每一版定版快照都是自洽的：第 1 段永远停在它被确认的样子，
    # 第 0 段与快照 revision 对应；逐版读取不可变。
    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    for pub in pubs:
        again = client.get(
            f"/manuscripts/{mid}/publications/{pub['edition']}"
        ).json()
        assert again == pub



# ---- 段落编排 -------------------------------------------------------------


def test_reorder_reverse_and_revise_revision_once(client):
    mid = _create(client, 3)
    for i in range(3):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    # 逆序排列：保留段的版本 / 译文 / 确认状态跟随原段
    r = _reorder(client, mid, 1, [_ref(2), _ref(1), _ref(0)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["segment_count"] == 3

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 2
    assert [s["source_text"] for s in draft["segments"]] == ["原文2", "原文1", "原文0"]
    assert [s["position"] for s in draft["segments"]] == [0, 1, 2]
    assert all(s["source_version"] == 1 for s in draft["segments"])
    assert all(s["status"] == "confirmed" for s in draft["segments"])
    assert [s["translation"]["target_text"] for s in draft["segments"]] == [
        "t2", "t1", "t0",
    ]

    # 位置改写接口按新位置工作：改的是"新位置 0"=旧位置 2 的段
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文2-改", "expected_revision": 2},
    )
    assert r.status_code == 200
    assert r.json()["source_version"] == 2
    assert r.json()["revision"] == 3


def test_reorder_drop_and_insert_segments(client):
    mid = _create(client, 3)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    _submit(client, mid, 1, "t1", 1)
    _confirm(client, mid, 1, 1)
    _submit(client, mid, 2, "t2", 1)
    _confirm(client, mid, 2, 1)

    # 新顺序：[新段, 旧2, 旧0] —— 旧1 被删除；新段版本 1、无译文
    r = _reorder(client, mid, 1, [_new("全新原文"), _ref(2), _ref(0)])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "manuscript_id": mid, "revision": 2, "segment_count": 3,
    }

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    by_pos = {s["position"]: s for s in draft["segments"]}
    assert by_pos[0]["source_text"] == "全新原文"
    assert by_pos[0]["source_version"] == 1
    assert by_pos[0]["status"] == "missing"
    assert by_pos[0]["translation"] is None
    assert by_pos[0]["stale_translation"] is None
    assert by_pos[1]["source_text"] == "原文2"
    assert by_pos[1]["status"] == "confirmed"
    assert by_pos[1]["translation"]["target_text"] == "t2"
    assert by_pos[2]["source_text"] == "原文0"
    assert by_pos[2]["translation"]["target_text"] == "t0"

    # 被删除的旧段彻底不存在：新位置越界与旧位置都不可访问
    assert client.put(
        f"/manuscripts/{mid}/segments/3/source",
        json={"text": "x", "expected_revision": 2},
    ).status_code == 404

    # 新段译文从版本 1 起步
    assert _submit(client, mid, 0, "new-t", 1).status_code == 200
    assert _confirm(client, mid, 0, 1).status_code == 200


def test_reorder_can_shrink_to_one_segment(client):
    mid = _create(client, 3)
    r = _reorder(client, mid, 1, [_ref(2)])
    assert r.status_code == 200, r.text
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert [s["source_text"] for s in draft["segments"]] == ["原文2"]
    assert draft["revision"] == 2

    # 稿件元数据的段计数同步
    meta = client.get(f"/manuscripts/{mid}").json()
    assert meta["segment_count"] == 1
    assert meta["revision"] == 2


def test_reorder_revision_conflict_is_full_rollback(client):
    mid = _create(client, 3)
    r = _reorder(client, mid, 99, [_ref(0)])
    assert r.status_code == 409

    # 过期拒绝不留任何改动
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 1
    assert [s["source_text"] for s in draft["segments"]] == ["原文0", "原文1", "原文2"]


def test_reorder_invalid_lists_rejected_entirely(client):
    mid = _create(client, 2)

    def rejected(items, status_code=422):
        before = client.get(f"/manuscripts/{mid}/draft").json()
        r = _reorder(client, mid, 1, items)
        assert r.status_code == status_code, r.text
        after = client.get(f"/manuscripts/{mid}/draft").json()
        assert before == after  # 整次拒绝，无部分改动

    # 旧段越界
    rejected([_ref(0), _ref(5)])
    # 旧段重复引用
    rejected([_ref(0), _ref(0)])
    # 新段空白文本
    rejected([_new("   "), _ref(0)])
    # 一个旧段都不保留（全新清单）
    rejected([_new("全新A"), _new("全新B")])
    # 空清单
    rejected([])
    # 一项同时给了两个引用
    rejected([{"source_position": 0, "source_text": "x"}])
    # 一项两个引用都不给
    rejected([{}])
    # expected_revision 非法
    r = _reorder(client, mid, 0, [_ref(0)])
    assert r.status_code == 422

    # 各种拒绝后修订号与段落原样不动
    assert client.get(f"/manuscripts/{mid}").json()["revision"] == 1


def test_reorder_new_unconfirmed_segment_blocks_publish(client):
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    # 先发布一版"调整前"的定版
    r = _publish(client, mid, 1)
    assert r.status_code == 201
    assert r.json()["edition"] == 1

    # 编排：两段保留段都已确认，但插入的新段没有译文
    r = _reorder(client, mid, 1, [_ref(0), _new("新增段"), _ref(1)])
    assert r.status_code == 200

    # 用旧修订号发布 -> 409
    assert _publish(client, mid, 1).status_code == 409
    # 新修订号 -> 422，只有新段是阻塞段
    r = _publish(client, mid, 2)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [
        {"position": 1, "source_version": 1},
    ]
    # 失败不增加版号
    editions = client.get(f"/manuscripts/{mid}/publications").json()
    assert [p["edition"] for p in editions] == [1]

    # 补齐新段译文并确认后可发布"调整后"的整篇
    _submit(client, mid, 1, "t-new", 1)
    _confirm(client, mid, 1, 1)
    r = _publish(client, mid, 2)
    assert r.status_code == 201, r.text
    pub2 = r.json()
    assert pub2["edition"] == 2
    assert pub2["revision"] == 2
    assert [s["source_text"] for s in pub2["segments"]] == ["原文0", "新增段", "原文1"]

    # 旧定版仍按调整前的原样读取
    old = client.get(f"/manuscripts/{mid}/publications/1").json()
    assert old["revision"] == 1
    assert [s["source_text"] for s in old["segments"]] == ["原文0", "原文1"]
    assert [s["target_text"] for s in old["segments"]] == ["t0", "t1"]


def test_reorder_carries_stale_state_and_translations(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    _submit(client, mid, 1, "t1", 1)
    _confirm(client, mid, 1, 1)

    # 第 0 段改版：已确认译文失效待复核
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )

    # 交换顺序：stale 状态、译文历史全部随段移动
    r = _reorder(client, mid, 2, [_ref(1), _ref(0)])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["source_text"] == "原文1"
    assert draft["segments"][0]["status"] == "confirmed"
    assert draft["segments"][1]["source_text"] == "原文0-v2"
    assert draft["segments"][1]["source_version"] == 2
    assert draft["segments"][1]["status"] == "stale"
    assert draft["segments"][1]["stale_translation"]["target_text"] == "t0"

    # 新位置 1 上必须依据当前版本 2 提交 / 确认，旧版本 1 被拒
    assert _submit(client, mid, 1, "x", 1).status_code == 409
    assert _submit(client, mid, 1, "t0-v2", 2).status_code == 200
    assert _confirm(client, mid, 1, 2).status_code == 200


def test_concurrent_reorder_and_publish_never_mix_layouts(client):
    """编排与发布交错：成功定版只能是调整前或调整后的完整一整篇。"""
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    barrier = threading.Barrier(2)

    def reorder():
        barrier.wait()
        r = _reorder(client, mid, 1, [_ref(1), _new("新增")])
        return r.status_code

    def publish():
        barrier.wait()
        r = _publish(client, mid, 1)
        return r.status_code, r.json() if r.status_code == 201 else None

    with ThreadPoolExecutor(max_workers=2) as ex:
        ro_code, pb = list(ex.map(lambda f: f(), [reorder, publish]))
        _, pb_body = pb

    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    if pb[0] == 201:
        # 发布抢锁成功 -> 定格调整前 rev1 的两段
        assert pb_body["revision"] == 1
        assert [s["source_text"] for s in pb_body["segments"]] == ["原文0", "原文1"]
        assert len(pubs) == 1
        # 编排随后仍成功（发布不推进修订号）
        assert ro_code == 200
    else:
        # 编排先提交 -> 发布依据过期修订号 409
        assert pb[0] == 409
        assert ro_code == 200
        assert pubs == []

    # 无论谁先，工作稿最终都是调整后的完整状态（rev2：旧1、新增）
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 2
    assert [s["source_text"] for s in draft["segments"]] == ["原文1", "新增"]


def test_validation_errors(client):
    r = client.post("/manuscripts", json={"title": "", "segments": ["a"]})
    assert r.status_code == 422
    r = client.post("/manuscripts", json={"title": "x", "segments": []})
    assert r.status_code == 422
    r = client.post("/manuscripts", json={"title": "x", "segments": ["   "]})
    assert r.status_code == 422
    assert client.get("/manuscripts/999").status_code == 404


# ---- 读者生效版 ------------------------------------------------------------


def _confirm_all(client, mid, n, ver):
    """为 0..n-1 各段提交并确认版本 ver 的译文，文本带位置标记。"""
    for pos in range(n):
        _submit(client, mid, pos, f"t{pos}-v{ver}", ver)
        assert _confirm(client, mid, pos, ver).status_code == 200


def _rewrite_resubmit_all(client, mid, n, old_rev, new_ver):
    """把每段原文改为新版本，重新提交 / 确认，返回新工作稿修订号。"""
    for pos in range(n):
        r = client.put(
            f"/manuscripts/{mid}/segments/{pos}/source",
            json={"text": f"原文{pos}-v{new_ver}", "expected_revision": old_rev},
        )
        assert r.status_code == 200, r.text
        old_rev = r.json()["revision"]
    _confirm_all(client, mid, n, new_ver)
    return old_rev


def _current(client, mid):
    return client.get(f"/manuscripts/{mid}/publications/current")


def _switch(client, mid, target, expected):
    return client.post(
        f"/manuscripts/{mid}/publications/current",
        json={"target_edition": target, "expected_active_edition": expected},
    )


def test_no_active_edition_before_first_publish(client):
    mid = _create(client, 2)
    _confirm_all(client, mid, 2, 1)  # 译文齐备但尚未发布

    # 稿件元数据：尚无生效版
    meta = client.get(f"/manuscripts/{mid}").json()
    assert meta["active_edition"] is None

    # 读取生效版：明确表示不存在，而不是 200 空体或工作稿内容
    r = _current(client, mid)
    assert r.status_code == 404
    assert "尚无生效" in r.json()["detail"]

    # 稿件本身不存在仍是 404
    assert _current(client, 999).status_code == 404


def test_publish_sets_active_edition_atomically_and_reader_sees_it(client):
    mid = _create(client, 2)
    _confirm_all(client, mid, 2, 1)

    r = _publish(client, mid, 1)
    assert r.status_code == 201, r.text
    assert client.get(f"/manuscripts/{mid}").json()["active_edition"] == 1

    cur = _current(client, mid)
    assert cur.status_code == 200
    assert cur.json()["edition"] == 1
    assert [s["target_text"] for s in cur.json()["segments"]] == ["t0-v1", "t1-v1"]

    # 发布第二版后，读者生效版立即跟随到第 2 版
    rev2 = _rewrite_resubmit_all(client, mid, 2, 1, 2)
    r = _publish(client, mid, rev2)
    assert r.status_code == 201 and r.json()["edition"] == 2
    cur = _current(client, mid)
    assert cur.json()["edition"] == 2
    assert [s["source_text"] for s in cur.json()["segments"]] == ["原文0-v2", "原文1-v2"]


def test_failed_publish_leaves_active_edition_untouched(client):
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    assert _publish(client, mid, 1).status_code == 201

    # 改写后工作稿 rev2、第 0 段旧译文 stale
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )

    # 修订冲突 -> 409，生效版仍是第 1 版
    assert _publish(client, mid, 1).status_code == 409
    assert _current(client, mid).json()["edition"] == 1

    # 译文未确认 -> 422 阻塞段，生效版仍是第 1 版，且没有新增版号
    r = _publish(client, mid, 2)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [{"position": 0, "source_version": 2}]
    assert _current(client, mid).json()["edition"] == 1
    assert [p["edition"] for p in client.get(f"/manuscripts/{mid}/publications").json()] == [1]

    # 补齐后发布第 2 版，生效版才推进
    _submit(client, mid, 0, "t0-v2", 2)
    _confirm(client, mid, 0, 2)
    assert _publish(client, mid, 2).status_code == 201
    assert _current(client, mid).json()["edition"] == 2


def test_switch_back_to_older_edition(client):
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    _publish(client, mid, 1)
    rev2 = _rewrite_resubmit_all(client, mid, 1, 1, 2)
    assert _publish(client, mid, rev2).status_code == 201
    assert _current(client, mid).json()["edition"] == 2

    # 切回第 1 版（预期当前生效为第 2 版）
    r = _switch(client, mid, target=1, expected=2)
    assert r.status_code == 200, r.text
    assert r.json()["edition"] == 1
    assert r.json()["segments"][0]["source_text"] == "原文0"
    assert _current(client, mid).json()["edition"] == 1

    # 再切到第 2 版，之后还能切回第 1 版：任一已发布旧版可反复切换
    assert _switch(client, mid, 2, 1).json()["edition"] == 2
    assert _switch(client, mid, 1, 2).json()["edition"] == 1
    assert _current(client, mid).json()["edition"] == 1


def test_switch_rejects_missing_target_and_stale_expected(client):
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    _publish(client, mid, 1)
    rev2 = _rewrite_resubmit_all(client, mid, 1, 1, 2)
    _publish(client, mid, rev2)  # 生效版 -> 2

    # 目标版号不存在 -> 404，生效版不变
    r = _switch(client, mid, target=99, expected=2)
    assert r.status_code == 404
    assert _current(client, mid).json()["edition"] == 2

    # 预期版号过期（当前已是 2，却预期 1）-> 409，生效版不变
    r = _switch(client, mid, target=1, expected=1)
    assert r.status_code == 409
    assert _current(client, mid).json()["edition"] == 2

    # 目标不存在 + 预期同时过期：仍是拒绝，生效版保持第 2 版
    assert _switch(client, mid, target=99, expected=1).status_code in (404, 409)
    assert _current(client, mid).json()["edition"] == 2

    # 带正确预期后切回第 1 版成功
    assert _switch(client, mid, target=1, expected=2).status_code == 200
    assert _current(client, mid).json()["edition"] == 1

    # 再用过期预期切换：当前已是 1，预期 2 -> 409，不能跳回第 2 版
    r = _switch(client, mid, target=2, expected=2)
    assert r.status_code == 409
    assert _current(client, mid).json()["edition"] == 1

    # 目标版号非法（<1）由请求体校验拦截
    r = client.post(
        f"/manuscripts/{mid}/publications/current",
        json={"target_edition": 0, "expected_active_edition": 1},
    )
    assert r.status_code == 422


def test_switch_when_never_published_requires_null_expected(client):
    mid = _create(client, 1)
    # 尚无生效版：target 不存在 -> 404，且非 null 的预期与 NULL 不匹配 -> 409；
    # 无论哪种都被拒绝，生效版仍不存在。
    assert _switch(client, mid, target=1, expected=None).status_code == 404
    assert _switch(client, mid, target=1, expected=1).status_code == 409
    assert _current(client, mid).status_code == 404
    assert client.get(f"/manuscripts/{mid}").json()["active_edition"] is None


def test_switch_does_not_touch_draft_revision_or_create_edition(client):
    mid = _create(client, 2)
    _confirm_all(client, mid, 2, 1)
    _publish(client, mid, 1)
    # 两段分别改写：工作稿修订号共推进 2 次（1 -> 3）
    rev_after = _rewrite_resubmit_all(client, mid, 2, 1, 2)
    assert rev_after == 3
    _publish(client, mid, rev_after)  # 生效版 2，工作稿 rev3
    editions_before = client.get(f"/manuscripts/{mid}/publications").json()

    r = _switch(client, mid, target=1, expected=2)
    assert r.status_code == 200

    # 工作稿修订号与内容不变
    meta = client.get(f"/manuscripts/{mid}").json()
    assert meta["revision"] == rev_after
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == rev_after
    assert [s["source_text"] for s in draft["segments"]] == ["原文0-v2", "原文1-v2"]

    # 没有生成新定版；既有按版号快照原样可读
    editions_after = client.get(f"/manuscripts/{mid}/publications").json()
    assert editions_after == editions_before
    one = client.get(f"/manuscripts/{mid}/publications/1").json()
    assert [s["target_text"] for s in one["segments"]] == ["t0-v1", "t1-v1"]

    # 切换后继续改写工作稿不影响刚切回的生效版
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v3", "expected_revision": rev_after},
    )
    assert r.status_code == 200 and r.json()["revision"] == rev_after + 1
    assert _current(client, mid).json()["edition"] == 1
    # 复核译文（submitted / confirmed）同样不影响生效版
    _submit(client, mid, 0, "t0-v3", 3)
    _confirm(client, mid, 0, 3)
    cur = _current(client, mid).json()
    assert cur["edition"] == 1
    assert cur["segments"][0]["source_text"] == "原文0"


def test_switch_to_same_edition_is_idempotent(client):
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    _publish(client, mid, 1)

    r = _switch(client, mid, target=1, expected=1)
    assert r.status_code == 200
    assert r.json()["edition"] == 1
    assert client.get(f"/manuscripts/{mid}").json()["active_edition"] == 1


def test_reader_always_sees_a_complete_snapshot_while_switching(client):
    """切换与切换交错期间反复读：生效版只能是某个完整发布快照。"""
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    _publish(client, mid, 1)
    rev2 = _rewrite_resubmit_all(client, mid, 1, 1, 2)
    _publish(client, mid, rev2)
    assert _current(client, mid).json()["edition"] == 2

    stop = threading.Event()
    observed: list[int] = []
    bad: list[dict] = []

    def reader():
        while not stop.is_set():
            r = _current(client, mid)
            if r.status_code == 200:
                body = r.json()
                observed.append(body["edition"])
                # 每一份生效版都必须与按版号读取的不可变快照逐字一致
                direct = client.get(
                    f"/manuscripts/{mid}/publications/{body['edition']}"
                ).json()
                if body != direct:
                    bad.append({"body": body, "direct": direct})

    def toggle(active_ref):
        """按调用方认为的当前版来回切；CAS 过期就放弃。"""
        r = _switch(client, mid, target=1, expected=active_ref)
        if r.status_code != 200:
            return active_ref
        r = _switch(client, mid, target=2, expected=1)
        if r.status_code != 200:
            return 1
        return 2

    t = threading.Thread(target=reader)
    t.start()
    try:
        ref = 2
        for _ in range(20):
            ref = toggle(ref)
    finally:
        stop.set()
        t.join()

    assert observed, "读者线程应当读到至少一次生效版"
    assert set(observed) <= {1, 2}
    assert bad == []


def test_concurrent_publish_and_switch_switch_never_overrides_new_edition(client):
    """发布与切换同时提交：过期切换不得覆盖其间新发布的第 2 版。

    发布成功时总是把生效版推进到自己；切换只在 expected 仍匹配时移动指针。
    因此若发布在切换之前提交，切换的 expected=1 必然过期（409），第 2 版
    不会被覆盖；若切换先提交，发布随后仍把生效版推进到第 2 版。
    """
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    assert _publish(client, mid, 1).status_code == 201  # 先有第 1 版生效

    # 准备 rev2：工作稿齐备，发布 rev2 与"切到第 1 版"同时发生
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )
    _submit(client, mid, 0, "t0-v2", 2)
    _confirm(client, mid, 0, 2)

    barrier = threading.Barrier(2)

    def publish_two():
        barrier.wait()
        r = _publish(client, mid, 2)
        return r.status_code

    def switch_to_one():
        barrier.wait()
        r = _switch(client, mid, target=1, expected=1)
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        pub_code, sw_code = list(ex.map(lambda f: f(), [publish_two, switch_to_one]))

    assert pub_code == 201
    assert sw_code in (200, 409)

    final_edition = _current(client, mid).json()["edition"]
    if sw_code == 409:
        # 发布先提交 -> 生效版已是 2，切换因预期过期被拒
        assert final_edition == 2
    else:
        # 切换先提交（2->1），发布在其后提交 -> 生效版最终仍是新发布的 2
        assert final_edition == 2
    # 关键不变量：无论交错顺序，新发布的第 2 版绝未被切换永久覆盖；
    # 唯二合法的指针历史是 1->2 或 1->2->1->2，读者读到的都对应完整快照。
    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    assert [p["edition"] for p in pubs] == [1, 2]


def test_stress_concurrent_publish_and_switch_final_points_at_latest(client):
    """压测：多轮顺序推进工作稿，每轮内"发布新版 / CAS 回切"并发。

    不变量：每轮发布成功总会把生效版推进到新发布版号；切换只在预期仍
    匹配时移动指针。因此全部轮次结束后，最终生效版必为最大已发布版号，
    且每次读取到的生效版都与按版号快照逐字一致。
    """
    mid = _create(client, 1)
    _confirm_all(client, mid, 1, 1)
    _publish(client, mid, 1)

    revision = 1
    for new_ver in range(2, 12):
        r = client.put(
            f"/manuscripts/{mid}/segments/0/source",
            json={"text": f"原文0-v{new_ver}", "expected_revision": revision},
        )
        assert r.status_code == 200, r.text
        revision = r.json()["revision"]
        _submit(client, mid, 0, f"t0-v{new_ver}", new_ver)
        _confirm(client, mid, 0, new_ver)

        before = client.get(f"/manuscripts/{mid}").json()["active_edition"]
        barrier = threading.Barrier(2)

        def do_publish():
            barrier.wait()
            return _publish(client, mid, revision).status_code

        def do_switch():
            barrier.wait()
            return _switch(client, mid, target=before, expected=before).status_code

        with ThreadPoolExecutor(max_workers=2) as ex:
            p_code, s_code = list(ex.map(lambda f: f(), [do_publish, do_switch]))

        assert p_code == 201
        assert s_code in (200, 409)

        # 读到的生效版必须与按版号读取的不可变快照完全一致
        cur = _current(client, mid).json()
        direct = client.get(
            f"/manuscripts/{mid}/publications/{cur['edition']}"
        ).json()
        assert cur == direct

    # 最后完成的发布版号最大，必为最终生效版。
    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    latest = max(p["edition"] for p in pubs)
    assert _current(client, mid).json()["edition"] == latest

"""配方 / 炉次草稿 / 归档 的持久化操作。

读操作只持进程内全局读锁（见 db.lock）；写操作一律先持「写互斥锁」
（见 db.write_lock）再持全局读锁，并在单条 SQLite 事务中整体提交，
保证「归档」「停用」「新建炉次」与「修改配方」按某一先后顺序生效；
读操作拿到的一定是同一版本下自洽的参数与结果。

「修改配方并重算草稿」与「新建炉次」是分两阶段的操作：锁内只取快照，
耗时的求解放在锁外（期间 GET 仍能读到上一整次提交的完整状态，新草稿
在提交前对读取不可见），随后再上锁、在单条事务里复核并发漂移并整体
提交；见 update_recipe 与 create_draft。连续竞争导致复核反复失效时，
兜底路径持写互斥锁走完 快照→求解→提交 全程：求解仍不持读锁（GET
不被阻塞），其他写操作在写互斥锁上排队，保证本次写操作必然整体提交
成功。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import reuse
from .db import connect, lock, write_lock
from .solver import Material, solve


# ---------------- 异常（路由层映射为 HTTP 状态码） ----------------


class StoreError(Exception):
    status_code = 400


class NotFound(StoreError):
    status_code = 404


class Conflict(StoreError):
    """状态冲突：草稿已归档，或携带的版本已过期（HTTP 409）。"""

    status_code = 409

    def __init__(self, detail: str, current_version: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.current_version = current_version


class Unprocessable(StoreError):
    """语义上无法处理：引用非法，或待归档草稿当前无解（HTTP 422）。"""

    status_code = 422


# ---------------- 小工具 ----------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _recipe_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "holdLow": row["hold_low"],
        "holdHigh": row["hold_high"],
        "duration": row["duration"],
        "tolerance": row["tolerance"],
        "status": row["status"],
        "version": row["version"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _material_snapshot(row) -> dict:
    """一份配方在某个时刻对应的材料参数（含默认名称）。"""
    return {
        "name": (row["name"] or "").strip() or f"配方{row['id']}",
        "holdLow": row["hold_low"],
        "holdHigh": row["hold_high"],
        "duration": row["duration"],
        "tolerance": row["tolerance"],
    }


def _to_material(row) -> Material:
    snap = _material_snapshot(row)
    return Material(
        name=snap["name"],
        hold_low=snap["holdLow"],
        hold_high=snap["holdHigh"],
        duration=snap["duration"],
        tolerance=snap["tolerance"],
    )


def _draft_dict(row) -> dict:
    """组装炉次草稿读取结构：状态、参数、版本、结果或无解原因。

    活动草稿的 materials 与 result/reason 是同一次求解一起写入的快照；
    已归档草稿则读取归档时冻结的快照，二者绝不会混用。
    """
    recipe_ids = json.loads(row["recipe_ids"])
    archived = row["archive_id"] is not None

    if archived:
        materials = json.loads(row["archived_materials_json"])
        result = json.loads(row["archived_result_json"])
        solvable = True
        reason = None
        recipe_versions = json.loads(row["recipe_versions_json"])
    else:
        materials = json.loads(row["materials_json"])
        solvable = bool(row["solvable"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        reason = row["reason"]
        recipe_versions = json.loads(row["recipe_versions_json"])

    return {
        "id": row["id"],
        "name": row["name"],
        "status": "archived" if archived else "active",
        "recipeIds": recipe_ids,
        "recipeVersions": recipe_versions,
        "version": row["archived_draft_version"] if archived else row["version"],
        "materials": materials,
        "solvable": solvable,
        "result": result,
        "reason": reason,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archivedAt": row["archived_at"],
    }


_DRAFT_SELECT = """
SELECT d.*, a.id AS archive_id, a.draft_version AS archived_draft_version,
       a.materials_json AS archived_materials_json,
       a.result_json AS archived_result_json, a.archived_at
FROM drafts d
LEFT JOIN archives a ON a.draft_id = d.id
"""


# ---------------- 配方 ----------------


def list_recipes() -> list[dict]:
    with lock(), connect() as conn:
        rows = conn.execute("SELECT * FROM recipes ORDER BY id").fetchall()
        return [_recipe_dict(r) for r in rows]


def get_recipe(recipe_id: int) -> dict:
    with lock(), connect() as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if row is None:
            raise NotFound(f"配方 {recipe_id} 不存在。")
        return _recipe_dict(row)


def create_recipe(
    name: str, hold_low: int, hold_high: int, duration: int, tolerance: int
) -> dict:
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            cur = conn.execute(
                """INSERT INTO recipes
                   (name, hold_low, hold_high, duration, tolerance,
                    version, created_at, updated_at)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (name, hold_low, hold_high, duration, tolerance, now, now),
            )
            rid = cur.lastrowid
        return get_recipe(rid)


# 锁外求解后允许的无锁重试次数；期间的并发写操作会让整批结果作废重算。
# 超出后转入兜底路径：持写互斥锁走完 快照→求解→提交 全程（求解仍不持
# 读锁），必然落在他次写操作之后，按某一先后顺序串行生效，永远不会把
# 写请求做丢。
_RECOMPUTE_MAX_ATTEMPTS = 5


def update_recipe(
    recipe_id: int,
    name: str,
    hold_low: int,
    hold_high: int,
    duration: int,
    tolerance: int,
) -> tuple[dict, list[dict]]:
    """整份修改配方（版本 +1），并原子地重算所有引用它的活动草稿。

    两阶段提交流程（单进程，无后台任务）：

    1. 锁内取快照：配方当前参数/版本与所有引用它的活动草稿、各引用配方
       当前参数/版本；
    2. **锁外**用新参数求解全部受影响草稿——此期间不持锁、未写任何数据，
       GET /api/recipes、GET /api/drafts、GET /api/drafts/{id} 读到的仍是
       上一整次提交的完整状态（旧参数 + 旧结果），绝看不到半成品；
    3. 锁内、单条 BEGIN IMMEDIATE 事务里复核并发漂移（配方或引用配方被
       他次修改、草稿被归档、出现引用该配方的新草稿），无漂移才把配方新
       参数/版本与全部草稿的新快照、结果/原因、版本一起 COMMIT；有漂移则
       整批作废，刷新快照重新求解。

    已归档炉次不受影响。引用草稿（无论新结果是否有解）版本一律 +1；
    无解草稿清除旧曲线并记录原因。求解异常 / 写库异常时本事务回滚，本次
    修改全部不生效。重试耗尽时转入兜底路径：持写互斥锁（读操作不经过它）
    走完 快照→求解→提交 全程——求解期间 GET 依旧立即读到上一整次提交
    的完整状态，其他写操作在写互斥锁上排队，快照到提交之间不会再插入
    他次写操作，保证请求最终成功且严格串行在他次修改之后。
    """
    # 阶段 1 + 2：锁内快照、锁外求解，允许在并发修改后整批重算
    for _ in range(_RECOMPUTE_MAX_ATTEMPTS):
        with lock():
            snapshot = _snapshot_recipe_recompute(recipe_id)
        prepared = _solve_recompute(
            snapshot,
            name,
            hold_low,
            hold_high,
            duration,
            tolerance,
        )

        with write_lock(), lock(), connect() as conn:
            with _tx(conn):
                affected = _commit_recipe_recompute(conn, recipe_id, prepared)
            if affected is not None:
                recipe = get_recipe(recipe_id)
                drafts = [get_draft(did) for did in affected]
                return recipe, drafts
            # 提交期发现并发漂移：整批结果作废，刷新快照后重新求解

    # 兜底：并发过于激烈时，持写互斥锁走完 快照→求解→提交 全程。求解期间
    # 不持全局读锁，GET 读到的仍是上一整次提交的完整状态；其他写操作在
    # 写互斥锁上排队，与本次修改严格按某一先后顺序生效，因此快照到提交
    # 之间不会再有漂移，复核一次通过、必然提交成功，不漏草稿、不覆盖、
    # 不改归档。
    with write_lock():
        while True:
            with lock():
                snapshot = _snapshot_recipe_recompute(recipe_id)
            prepared = _solve_recompute(
                snapshot,
                name,
                hold_low,
                hold_high,
                duration,
                tolerance,
            )
            with lock(), connect() as conn:
                with _tx(conn):
                    affected = _commit_recipe_recompute(conn, recipe_id, prepared)
                if affected is not None:
                    recipe = get_recipe(recipe_id)
                    drafts = [get_draft(did) for did in affected]
                    return recipe, drafts
                # 持写互斥锁期间没有其他写操作生效，复核必然通过；
                # 循环仅为防御，正常只走一遍


def set_recipe_status(recipe_id: int, status: str) -> dict:
    """停用（disabled）/ 恢复（active）配方，只切换状态本身。

    不改变材料参数，不增加配方或任何草稿的版本，也不重算已有炉次；
    重复停用 / 恢复为幂等。与新建炉次并发时由写互斥锁串行化：停用先
    生效，则引用它的新建因「已停用」以 422 失败。
    """
    assert status in ("active", "disabled")  # 路由层只传这两个值
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            row = conn.execute(
                "SELECT id FROM recipes WHERE id=?", (recipe_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"配方 {recipe_id} 不存在。")
            conn.execute(
                "UPDATE recipes SET status=? WHERE id=?", (status, recipe_id)
            )
        return get_recipe(recipe_id)


def _snapshot_recipe_recompute(recipe_id: int) -> dict:
    """在已持有全局锁时读取修改所需快照（只读，不写任何数据）。

    包含：目标配方当前行、所有引用它的活动草稿，以及这些草稿各自引用
    配方的当前行。全部读取自同一连接、同一锁时点，状态彼此自洽。
    """
    with connect() as conn:
        target = conn.execute(
            "SELECT * FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        if target is None:
            raise NotFound(f"配方 {recipe_id} 不存在。")
        target = dict(target)

        drafts = [
            dict(d)
            for d in conn.execute(
                """SELECT d.* FROM drafts d
                   LEFT JOIN archives a ON a.draft_id = d.id
                   WHERE a.id IS NULL ORDER BY d.id"""
            ).fetchall()
            if recipe_id in json.loads(d["recipe_ids"])
        ]
        ref_ids = {rid for d in drafts for rid in json.loads(d["recipe_ids"])}
        ref_ids.add(recipe_id)
        placeholders = ",".join("?" * len(ref_ids))
        recipes = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"SELECT * FROM recipes WHERE id IN ({placeholders})",
                list(ref_ids),
            ).fetchall()
        }
    return {"target": target, "drafts": drafts, "recipes": recipes}


def _solve_recompute(
    snapshot: dict,
    name: str,
    hold_low: int,
    hold_high: int,
    duration: int,
    tolerance: int,
) -> dict:
    """锁外按新参数求解全部受影响草稿；只做计算，不碰数据库。

    目标配方行用新参数覆盖（版本记为 base_version+1），其余引用配方沿用
    快照值。异常直接向上抛出：此时尚未写入任何数据，本次修改天然全不生效。
    """
    recipe_id = snapshot["target"]["id"]
    base_version = snapshot["target"]["version"]

    target = dict(snapshot["target"])
    target.update(
        name=name,
        hold_low=hold_low,
        hold_high=hold_high,
        duration=duration,
        tolerance=tolerance,
        version=base_version + 1,
    )
    recipes = dict(snapshot["recipes"])
    recipes[recipe_id] = target

    prepared_drafts: list[dict] = []
    for d in snapshot["drafts"]:
        refs = json.loads(d["recipe_ids"])
        ordered = [recipes[r] for r in refs]
        materials = [_to_material(r) for r in ordered]
        snapshot_params = [_material_snapshot(r) for r in ordered]
        versions = {str(r["id"]): r["version"] for r in ordered}
        outcome = solve(materials)
        if outcome.get("solvable"):
            solvable, result_json, reason = 1, _dumps(outcome), None
        else:
            # 无解：仍保存，但清除旧曲线并记录原因
            solvable, result_json, reason = 0, None, outcome.get("reason")
        prepared_drafts.append(
            {
                "id": d["id"],
                "base_version": d["version"],
                # 快照时点数据库中各引用配方的版本（目标配方此刻还未 +1）
                "base_ref_versions": {
                    str(rid): snapshot["recipes"][rid]["version"] for rid in refs
                },
                "materials_json": _dumps(snapshot_params),
                "recipe_versions_json": _dumps(versions),
                "solvable": solvable,
                "result_json": result_json,
                "reason": reason,
            }
        )

    return {
        "recipe_id": recipe_id,
        "base_version": base_version,
        "fields": (name, hold_low, hold_high, duration, tolerance),
        "drafts": prepared_drafts,
    }


def _commit_recipe_recompute(conn, recipe_id: int, prepared: dict):
    """锁内、单条事务内复核漂移并整体提交；返回受影响草稿 ID。

    任一复核不过即说明快照后有他次写操作生效（并发修改共享配方、创建引用
    炉次、归档等），返回 None 让调用方整批作废、刷新快照重算，绝不覆盖
    他次修改、不漏算新草稿、不触碰已归档冻结方案。复核通过后，配方新参数
    /版本与全部草稿的新参数快照、结果/原因、版本在同一事务里一起生效。
    """
    target = conn.execute(
        "SELECT * FROM recipes WHERE id=?", (recipe_id,)
    ).fetchone()
    if target is None:
        # 配方没有删除接口；缺失意味着状态已不可预期，直接回滚
        raise NotFound(f"配方 {recipe_id} 不存在。")
    if target["version"] != prepared["base_version"]:
        return None  # 配方已被他次修改 / 停用恢复之外的写操作抢先生效

    # 受影响草稿集合必须与快照一致：不多（不能改写已归档/新建外的草稿）、
    # 不少（引用该配方的新草稿必须纳入）
    current_active = [
        d
        for d in conn.execute(
            """SELECT d.* FROM drafts d
               LEFT JOIN archives a ON a.draft_id = d.id
               WHERE a.id IS NULL ORDER BY d.id"""
        ).fetchall()
        if recipe_id in json.loads(d["recipe_ids"])
    ]
    by_id = {d["id"]: d for d in current_active}
    expected_ids = {d["id"] for d in prepared["drafts"]}
    if set(by_id) != expected_ids:
        return None  # 快照后有草稿被归档，或有引用该配方的新炉次创建

    for pd in prepared["drafts"]:
        cur = by_id[pd["id"]]
        if cur["version"] != pd["base_version"]:
            return None  # 该草稿已被他次修改（其他共享配方的修改）重算
        # 该草稿引用的每个配方版本都必须与求解快照时一致（目标配方此刻
        # 也尚未 +1）；若有其他共享配方被改过，本批结果必须作废重算
        for rid, snapshot_version in pd["base_ref_versions"].items():
            row = conn.execute(
                "SELECT version FROM recipes WHERE id=?", (int(rid),)
            ).fetchone()
            if row is None or row["version"] != snapshot_version:
                return None

    # 复核通过：配方与全部受影响草稿在同一事务内一起生效
    name, hold_low, hold_high, duration, tolerance = prepared["fields"]
    now = _now()
    conn.execute(
        """UPDATE recipes SET name=?, hold_low=?, hold_high=?, duration=?,
               tolerance=?, version=version+1, updated_at=?
           WHERE id=?""",
        (name, hold_low, hold_high, duration, tolerance, now, recipe_id),
    )
    for pd in prepared["drafts"]:
        conn.execute(
            """UPDATE drafts SET materials_json=?, recipe_versions_json=?,
                   solvable=?, result_json=?, reason=?, version=version+1,
                   updated_at=?
               WHERE id=?""",
            (
                pd["materials_json"],
                pd["recipe_versions_json"],
                pd["solvable"],
                pd["result_json"],
                pd["reason"],
                now,
                pd["id"],
            ),
        )
    return sorted(by_id)


# ---------------- 炉次草稿 ----------------


def list_drafts() -> list[dict]:
    with lock(), connect() as conn:
        rows = conn.execute(_DRAFT_SELECT + " ORDER BY d.id").fetchall()
        return [_draft_dict(r) for r in rows]


def get_draft(draft_id: int) -> dict:
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        return _draft_dict(row)


def create_draft(name: str, recipe_ids: list[int]) -> dict:
    """按顺序引用 2~3 个不同的可用配方；引用不可改，从 20°C 空记录求解。

    与「修改配方」同款的两阶段流程（单进程，无后台任务）：

    1. 锁内取引用配方快照并校验：不存在或已停用 => 422，不保存任何数据；
    2. **锁外**求解——此期间不持锁、未写任何数据，GET /api/recipes、
       GET /api/drafts、GET /api/drafts/{id} 照常立即读到上一整次提交的
       完整状态，新草稿在提交前对任何读取都不可见；
    3. 锁内、单条 BEGIN IMMEDIATE 事务里复核漂移（引用配方被停用 => 422；
       被他次修改 => 整批作废重算）后插入：参数快照、求解结果与引用版本
       来自同一次求解、同一事务，绝不会出现参数与结果错配。

    POST 仍等求解与保存完成后才返回 201；求解 / 写库异常不会留下新草稿。
    与「修改配方」并发时按某一先后顺序串行生效：新草稿先提交，则随后
    提交的配方修改会在复核时发现它并纳入重算；配方修改先提交，则本次
    新建复核发现版本漂移，按最新参数重新求解后再保存。连续竞争导致复核
    反复失效时转入兜底路径：持写互斥锁（读操作不经过它）走完
    快照→求解→提交 全程，必然提交成功。
    """
    # 阶段 1 + 2：锁内快照、锁外求解，允许在并发修改后整批重算
    for _ in range(_RECOMPUTE_MAX_ATTEMPTS):
        with lock():
            snapshot = _snapshot_draft_create(recipe_ids)
        prepared = _solve_draft_create(snapshot, name, recipe_ids)

        with write_lock(), lock(), connect() as conn:
            with _tx(conn):
                draft_id = _commit_draft_create(conn, prepared)
            if draft_id is not None:
                return get_draft(draft_id)
            # 提交期发现引用配方被他次修改：整批作废，刷新快照后重新求解

    # 兜底：并发过于激烈时，持写互斥锁走完 快照→求解→提交 全程。求解期间
    # 不持全局读锁，GET 读到的仍是上一整次提交的完整状态；其他写操作在
    # 写互斥锁上排队，与本次新建严格按某一先后顺序生效，因此快照到提交
    # 之间不会再有漂移，复核一次通过、必然提交成功。
    with write_lock():
        while True:
            with lock():
                snapshot = _snapshot_draft_create(recipe_ids)
            prepared = _solve_draft_create(snapshot, name, recipe_ids)
            with lock(), connect() as conn:
                with _tx(conn):
                    draft_id = _commit_draft_create(conn, prepared)
                if draft_id is not None:
                    return get_draft(draft_id)
                # 持写互斥锁期间没有其他写操作生效，复核必然通过；
                # 循环仅为防御，正常只走一遍


def _snapshot_draft_create(recipe_ids: list[int]) -> dict:
    """在已持有全局锁时读取新建炉次所需快照（只读，不写任何数据）。

    引用不存在或已停用 => 422（不保存）；全部引用配方读取自同一连接、
    同一锁时点，状态彼此自洽。
    """
    with connect() as conn:
        placeholders = ",".join("?" * len(recipe_ids))
        rows = conn.execute(
            f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
        ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    missing = [rid for rid in recipe_ids if rid not in by_id]
    if missing:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, missing))} 不存在，无法引用。"
        )
    ordered = [by_id[rid] for rid in recipe_ids]
    # 停用配方不可用于新建炉次（不保存草稿）；已有引用不受影响
    disabled = [r["id"] for r in ordered if r["status"] != "active"]
    if disabled:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, disabled))} 已停用，"
            "不能用于新建炉次。"
        )
    return {"recipes": ordered}


def _solve_draft_create(snapshot: dict, name: str, recipe_ids: list[int]) -> dict:
    """锁外按快照参数求解新炉次；只做计算，不碰数据库。

    无解草稿同样保存（不保留任何曲线，只记录原因）。异常直接向上抛出：
    此时尚未写入任何数据，不会留下新草稿。
    """
    ordered = snapshot["recipes"]
    materials = [_to_material(r) for r in ordered]
    # 从 20°C 空记录求解（executed=None），复用原求解规则
    outcome = solve(materials)
    if outcome.get("solvable"):
        solvable, result_json, reason = 1, _dumps(outcome), None
    else:
        solvable, result_json, reason = 0, None, outcome.get("reason")
    return {
        "name": name,
        "recipe_ids": list(recipe_ids),
        "materials_json": _dumps([_material_snapshot(r) for r in ordered]),
        "recipe_versions_json": _dumps(
            {str(r["id"]): r["version"] for r in ordered}
        ),
        # 快照时点各引用配方的版本，提交时据此复核漂移
        "base_ref_versions": {r["id"]: r["version"] for r in ordered},
        "solvable": solvable,
        "result_json": result_json,
        "reason": reason,
    }


def _commit_draft_create(conn, prepared: dict) -> int | None:
    """锁内、单条事务内复核漂移并插入新炉次；返回新草稿 ID。

    求解期间引用配方被停用 => 422（停用先生效，不保存）；被他次修改 =>
    返回 None 让调用方整批作废、按最新参数重新求解，保证保存的参数快照、
    结果与引用版本和提交时的配方状态严格同版。
    """
    recipe_ids = prepared["recipe_ids"]
    placeholders = ",".join("?" * len(recipe_ids))
    rows = conn.execute(
        f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    missing = [rid for rid in recipe_ids if rid not in by_id]
    if missing:  # 配方没有删除接口；缺失意味着状态已不可预期，回滚
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, missing))} 不存在，无法引用。"
        )
    ordered = [by_id[rid] for rid in recipe_ids]
    disabled = [r["id"] for r in ordered if r["status"] != "active"]
    if disabled:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, disabled))} 已停用，"
            "不能用于新建炉次。"
        )
    for r in ordered:
        if r["version"] != prepared["base_ref_versions"][r["id"]]:
            return None  # 求解期间配方被他次修改：整批作废，按最新参数重算

    now = _now()
    cur = conn.execute(
        """INSERT INTO drafts
           (name, recipe_ids, version, recipe_versions_json,
            materials_json, solvable, result_json, reason,
            created_at, updated_at)
           VALUES (?,?,1,?,?,?,?,?,?,?)""",
        (
            prepared["name"],
            _dumps(recipe_ids),
            prepared["recipe_versions_json"],
            prepared["materials_json"],
            prepared["solvable"],
            prepared["result_json"],
            prepared["reason"],
            now,
            now,
        ),
    )
    return cur.lastrowid


def archive_draft(draft_id: int, expected_version: int) -> dict:
    """归档核准的炉次方案。

    - 携带所查看的草稿版本：与当前版本不一致 => 409（附当前版本）；
    - 草稿当前无解 => 422（不改变任何状态）；
    - 已归档 => 409；
    - 成功则冻结当时的材料参数、草稿版本与完整求解结果；此后配方修改
      不再影响该炉次。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            row = conn.execute(
                _DRAFT_SELECT + " WHERE d.id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            if row["archive_id"] is not None:
                raise Conflict("该炉次已归档，不能重复归档。", row["version"])
            if row["version"] != expected_version:
                raise Conflict(
                    f"炉次草稿已过期：你查看的是版本 {expected_version}，"
                    f"当前为版本 {row['version']}，请重新读取后再归档。",
                    row["version"],
                )
            if not row["solvable"]:
                raise Unprocessable(
                    row["reason"] or "该炉次当前无可行曲线，无法归档。"
                )

            conn.execute(
                """INSERT INTO archives
                   (draft_id, draft_version, materials_json, result_json, archived_at)
                   VALUES (?,?,?,?,?)""",
                (
                    draft_id,
                    row["version"],
                    row["materials_json"],
                    row["result_json"],
                    now,
                ),
            )
        return get_draft(draft_id)


# ---------------- 电费试算（只读归档冻结曲线） ----------------


def get_archived_curve(draft_id: int) -> dict:
    """读取已归档炉次的冻结曲线用于电费试算。

    - 炉次不存在 => NotFound（404）；
    - 炉次存在但未归档（活动草稿 / 无解草稿）=> Unprocessable（422）；
    - 已归档 => 返回冻结版本与冻结温度序列。只读，绝无任何写入。
    """
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        if row["archive_id"] is None:
            raise Unprocessable("该炉次尚未归档，电费试算只能针对已归档的冻结曲线。")
        result = json.loads(row["archived_result_json"])
        return {
            "id": draft_id,
            "version": row["archived_draft_version"],
            "temperatures": list(result["temperatures"]),
        }


# ---------------- 原曲线复用检查（只读，不重新求解） ----------------


def check_archived_reuse(draft_id: int) -> dict:
    """核验已归档炉次的冻结曲线能否用于各配方的**当前**参数与状态。

    - 炉次不存在 => NotFound（404）；
    - 炉次存在但未归档（活动草稿 / 无解草稿）=> Unprocessable（422）；
    - 已归档 => 在同一把锁、同一条连接内读取冻结曲线与全部引用配方的
      当前参数/状态（同一读取时点），按原引用顺序逐项核验并给出整体
      可复用结论。纯只读：不改配方、草稿或归档，也不重新求解。
    """
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        if row["archive_id"] is None:
            raise Unprocessable(
                "该炉次尚未归档，复用检查只能针对已归档的冻结曲线。"
            )

        recipe_ids = json.loads(row["recipe_ids"])
        result = json.loads(row["archived_result_json"])
        temperatures = list(result["temperatures"])

        # 按原配方引用顺序读取当前参数与状态（引用创建后不可改，配方不会被删除）
        placeholders = ",".join("?" * len(recipe_ids))
        rows = conn.execute(
            f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[rid] for rid in recipe_ids]

        items: list[dict] = []
        for r in ordered:
            # 停用配方同样完成温度检查；其状态只影响整体可复用结论
            check = reuse.check_item(temperatures, _to_material(r))
            items.append(
                {
                    "recipeId": r["id"],
                    "name": _material_snapshot(r)["name"],
                    "version": r["version"],
                    "status": r["status"],
                    **check,
                }
            )

        # 全部保温达标、无任何超限且配方均可用时，整体才可复用
        reusable = all(
            it["holdOk"]
            and it["toleranceBreachStep"] is None
            and it["status"] == "active"
            for it in items
        )
        return {
            "draftId": draft_id,
            "version": row["archived_draft_version"],
            "stepCount": len(temperatures),
            "temperatures": temperatures,
            "items": items,
            "reusable": reusable,
        }

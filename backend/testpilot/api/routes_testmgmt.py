"""Test-case import (CSV/Excel) and test runs (planned executions of many cases)."""
from __future__ import annotations

import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import access, artifacts
from .. import importer as im
from ..db import get_db
from ..models import ITEM_STATUSES, Project, Run, TestCase, TestRun, TestRunItem, User, utcnow
from .deps import audit, member
from .routes_runs import queue_case_run

router = APIRouter(tags=["test-management"])

# Project permissions (access.PROJECT_MATRIX) used here:
#   project.read -> view test runs, export          case.write  -> import cases, undo an import
#   run.execute  -> create/edit test runs, record results, queue automated runs


def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    """A project of the caller's organization the caller may access now (404 otherwise, 403 if the
    permission is missing). Every query below is additionally scoped by me.tenant_id."""
    return access.require(db, me, db.get(Project, pid), perm)


async def _read(file: UploadFile) -> bytes:
    data = await file.read(im.MAX_BYTES + 1)
    if len(data) > im.MAX_BYTES:
        raise HTTPException(413, f"File is larger than {im.MAX_BYTES // (1024 * 1024)} MB")
    return data


def _json_form(raw: str, what: str) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        raise HTTPException(422, f"{what} is not valid JSON")
    if not isinstance(v, dict):
        raise HTTPException(422, f"{what} must be an object")
    return v


def _parse(filename: str, data: bytes, sheet: str) -> im.Table:
    try:
        return im.parse_file(filename, data, sheet)
    except im.ImportError_ as e:
        raise HTTPException(422, str(e))


# ============================================================================ import
@router.get("/imports/fields")
def import_fields(_: User = Depends(member)):
    return [{"key": f.key, "label": f.label, "target": f.target, "help": f.help, "required": f.required,
             "groupable": f.groupable} for f in im.FIELDS]


@router.get("/imports/template")
def import_template(format: str = "xlsx", _: User = Depends(member)):
    if format == "csv":
        body, mime, ext = im.template_csv(), "text/csv; charset=utf-8", "csv"
    elif format == "xlsx":
        body, mime, ext = im.template_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    else:
        raise HTTPException(422, "format must be xlsx or csv")
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="testpilot-test-case-template.{ext}"',
        "Cache-Control": "private, no-store"})


def _existing_ids(db: Session, pid: int, tenant_id: int) -> dict[str, TestCase]:
    rows = db.scalars(select(TestCase).where(TestCase.tenant_id == tenant_id, TestCase.project_id == pid, TestCase.archived.is_(False),
                                             TestCase.external_id != "")).all()
    return {c.external_id.lower(): c for c in rows}


def _custom_attrs(mapping: dict[str, str]) -> list[str]:
    return [v[5:] for v in mapping.values() if v.startswith("attr:")]


@router.post("/projects/{pid}/imports/preview")
async def import_preview(pid: int, file: UploadFile = File(...), sheet: str = Form(""), mapping: str = Form(""),
                         db: Session = Depends(get_db), me: User = Depends(member)):
    """Reads the file and reports what an import would do. Writes nothing."""
    _project(db, pid, me, "case.write")
    data = await _read(file)
    t = _parse(file.filename or "", data, sheet)
    given = _json_form(mapping, "mapping")
    mp = {h: given.get(h, "") for h in t.headers} if given else im.suggest_mapping(t.headers)
    out: dict = {
        "file": file.filename, "sheets": t.sheets, "sheet": t.sheet, "headers": t.headers,
        "row_count": len(t.rows), "truncated": t.truncated, "max_rows": im.MAX_ROWS,
        "mapping": mp, "suggested": im.suggest_mapping(t.headers),
        "sample": [dict(zip(t.headers, r)) for r in t.rows[:5]],
    }
    try:
        m = im.apply_mapping(t, mp)
    except im.ImportError_ as e:
        out["mapping_error"] = str(e)
        return out
    existing = _existing_ids(db, pid, me.tenant_id)
    matches = sum(1 for r in m.records if r.case.get("external_id", "").lower() in existing)
    results: dict[str, int] = {}
    for r in m.records:
        if "status" in r.result:
            results[r.result["status"]] = results.get(r.result["status"], 0) + 1
    opts = im.group_options(m.records, _custom_attrs(mp))
    groups = {o["field"]: im.groups_for(((r.case, r.attributes) for r in m.records), [o["field"]])
              for o in opts if o["distinct"] <= 200}
    out.update(
        valid=len(m.records), errors=m.errors[:100], error_count=len(m.errors), warnings=m.warnings[:100],
        warning_count=len(m.warnings), existing_matches=matches, new_cases=len(m.records) - matches,
        results=results, group_options=opts, groups=groups,
        recommended_group_by=im.recommend_grouping(opts, len(m.records)),
    )
    return out


class RunPlan(BaseModel):
    mode: Literal["none", "single", "group"] = "group"
    group_by: list[str] = Field(default_factory=list, max_length=2)
    groups: list[str] | None = None            # selected group values; None = all
    name: str = Field(default="", max_length=150)   # single-run name, or prefix for grouped runs
    apply_results: bool = True                 # copy Result / Actual Result / Executed By / On into the run(s)


class ImportOptions(BaseModel):
    sheet: str = ""
    mapping: dict[str, str]
    duplicates: Literal["update", "skip", "create"] = "update"
    run_plan: RunPlan = Field(default_factory=RunPlan)
    confirm: bool = False


def _apply_case(c: TestCase, rec: im.Record, merge_attrs: bool) -> None:
    for k, v in rec.case.items():
        setattr(c, k, v)
    c.attributes = {**(c.attributes or {}), **rec.attributes} if merge_attrs else dict(rec.attributes)


def _item_from_record(rec: im.Record | None, c: TestCase, pos: int, apply_results: bool) -> TestRunItem:
    it = TestRunItem(tenant_id=c.tenant_id, test_case_id=c.id, position=pos, status="untested", assigned_to=c.assigned_to or "")
    if rec and apply_results:
        res = rec.result
        it.status = res.get("status", "untested")
        it.actual_result = res.get("actual_result", "")
        it.executed_by_name = res.get("executed_by", "")[:120]
        raw_on = res.get("executed_on", "")
        it.executed_at = im.parse_date(raw_on)
        if raw_on and not it.executed_at:
            it.notes = f"Executed on: {raw_on}"
        if it.status != "untested" and not it.executed_at and (it.executed_by_name or it.actual_result):
            it.executed_at = utcnow()
    return it


def _create_runs(db: Session, pid: int, me: User, plan: RunPlan, cases: list[tuple[TestCase, im.Record | None]],
                 source: str, batch: str, default_name: str) -> list[TestRun]:
    if plan.mode == "none" or not cases:
        return []
    for k in plan.group_by:
        if k not in im.GROUPABLE and not k.startswith("attr:"):
            raise HTTPException(422, f"Cannot group by '{k}'")
    if plan.mode == "group" and not plan.group_by:
        raise HTTPException(422, "Choose a field to group test runs by")
    buckets: dict[str, list[tuple[TestCase, im.Record | None]]] = {}
    if plan.mode == "single":
        buckets[""] = cases
    else:
        for c, rec in cases:
            src = rec.case if rec else {k: getattr(c, k, "") for k in im.FIELD if hasattr(c, k)}
            attrs = rec.attributes if rec else (c.attributes or {})
            buckets.setdefault(im.group_key(src, attrs, plan.group_by), []).append((c, rec))
        if plan.groups is not None:
            wanted = set(plan.groups)
            buckets = {k: v for k, v in buckets.items() if k in wanted}
    if len(buckets) > 300:
        raise HTTPException(422, f"That would create {len(buckets)} test runs; choose a coarser grouping (max 300)")
    label = im.group_label(plan.group_by) if plan.mode == "group" else ""
    prefix = plan.name.strip() or default_name
    runs = []
    for key, members in buckets.items():
        name = prefix if plan.mode == "single" else (f"{prefix} – {key}" if prefix else key)
        tr = TestRun(tenant_id=me.tenant_id, project_id=pid, name=name[:200], source=source, created_by=me.id, import_batch=batch,
                     group_label=f"{label} = {key}"[:300] if label else "")
        db.add(tr)
        db.flush()
        for pos, (c, rec) in enumerate(members):
            it = _item_from_record(rec, c, pos, plan.apply_results)
            it.test_run_id = tr.id
            db.add(it)
        runs.append(tr)
    return runs


@router.post("/projects/{pid}/imports/commit", status_code=201)
async def import_commit(pid: int, request: Request, file: UploadFile = File(...), options: str = Form(...),
                        db: Session = Depends(get_db), me: User = Depends(member)):
    """Imports the file with the confirmed mapping and creates the agreed test runs."""
    p = _project(db, pid, me, "case.write")
    try:
        opts = ImportOptions.model_validate(_json_form(options, "options"))
    except ValidationError as e:
        raise HTTPException(422, "Invalid options: " + "; ".join(x["msg"] for x in e.errors()[:5]))
    if not opts.confirm:
        raise HTTPException(422, "Review the import summary and confirm before importing")
    data = await _read(file)
    t = _parse(file.filename or "", data, opts.sheet)
    try:
        m = im.apply_mapping(t, opts.mapping)
    except im.ImportError_ as e:
        raise HTTPException(422, str(e))
    if not m.records:
        raise HTTPException(422, "No importable rows were found")
    batch = uuid.uuid4().hex[:12]
    existing = _existing_ids(db, pid, me.tenant_id)
    created = updated = skipped = 0
    touched: list[tuple[TestCase, im.Record]] = []
    for rec in m.records:
        ext = rec.case.get("external_id", "").lower()
        c = existing.get(ext) if ext else None
        if c and opts.duplicates == "skip":
            skipped += 1
            touched.append((c, rec))  # still part of the test run(s)
            continue
        if c and opts.duplicates == "update":
            _apply_case(c, rec, merge_attrs=True)
            updated += 1
        else:
            c = TestCase(tenant_id=me.tenant_id, project_id=pid, source="import", created_by=me.id, steps=[], import_batch=batch,
                         description="", tags=[])
            _apply_case(c, rec, merge_attrs=False)
            db.add(c)
            created += 1
        touched.append((c, rec))
    db.flush()
    # organizations with their own storage: a versioned copy of every case this import created or changed
    for c in {id(c): c for c, _ in touched}.values():
        if opts.duplicates != "skip" or c.import_batch == batch:
            artifacts.queue_case_version(db, c, me.id)
    stem = re.sub(r"\.(csv|xlsx|xlsm)$", "", file.filename or "Import", flags=re.I)[:80]
    runs = _create_runs(db, pid, me, opts.run_plan, touched, "import", batch, stem)
    audit(db, request, me, "cases_imported", p.name, project_id=pid, file=(file.filename or "")[:200],
          created=created, updated=updated, skipped=skipped, rows_with_errors=len(m.errors), test_runs=len(runs),
          batch=batch)
    db.commit()
    return {"batch": batch, "created": created, "updated": updated, "skipped": skipped,
            "errors": m.errors[:100], "error_count": len(m.errors),
            "test_runs": [{"id": r.id, "name": r.name} for r in runs]}


@router.post("/projects/{pid}/imports/{batch}/undo")
def import_undo(pid: int, batch: str, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    """Archives the cases an import created and deletes the test runs it created.
    Cases that the import updated keep their new values."""
    _project(db, pid, me, "case.write")
    if not re.fullmatch(r"[0-9a-f]{12}", batch):
        raise HTTPException(404, "Import not found")
    cases = db.scalars(select(TestCase).where(TestCase.tenant_id == me.tenant_id, TestCase.project_id == pid,
                                              TestCase.import_batch == batch,
                                              TestCase.archived.is_(False))).all()
    runs = db.scalars(select(TestRun).where(TestRun.tenant_id == me.tenant_id, TestRun.project_id == pid,
                                            TestRun.import_batch == batch)).all()
    for c in cases:
        c.archived = True
    for r in runs:
        db.delete(r)
    audit(db, request, me, "import_undone", batch, project_id=pid, cases=len(cases), test_runs=len(runs))
    db.commit()
    return {"archived_cases": len(cases), "deleted_test_runs": len(runs)}


# ============================================================================ test runs
def _counts(db: Session, run_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in run_ids}
    if not run_ids:
        return out
    for rid, st, n in db.execute(select(TestRunItem.test_run_id, TestRunItem.status, func.count())
                                 .where(TestRunItem.test_run_id.in_(run_ids))
                                 .group_by(TestRunItem.test_run_id, TestRunItem.status)).all():
        out[rid][st] = n
    return out


def _tr_out(tr: TestRun, counts: dict[str, int], creator: str | None = None) -> dict:
    total = sum(counts.values())
    done = total - counts.get("untested", 0)
    return {"id": tr.id, "project_id": tr.project_id, "name": tr.name, "description": tr.description,
            "status": tr.status, "source": tr.source, "group_label": tr.group_label, "import_batch": tr.import_batch,
            "created_at": tr.created_at, "updated_at": tr.updated_at, "closed_at": tr.closed_at,
            "created_by": creator, "total": total, "counts": {s: counts.get(s, 0) for s in ITEM_STATUSES},
            "progress": round(100 * done / total) if total else 0}


def _get_tr(db: Session, tid: int, me: User, perm: str = "project.read") -> TestRun:
    """A test run of the caller's organization in a project the caller may access now; otherwise not found."""
    tr = db.get(TestRun, tid)
    if not tr or tr.tenant_id != me.tenant_id:
        raise HTTPException(404, "Test run not found")
    try:
        access.require(db, me, db.get(Project, tr.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Test run not found")
        raise
    return tr


@router.get("/projects/{pid}/test-runs")
def list_test_runs(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    runs = list(db.scalars(select(TestRun).where(TestRun.tenant_id == me.tenant_id, TestRun.project_id == pid).order_by(TestRun.id)).all())
    # newest first, but runs created together (one import / one grouping) keep their file order
    first: dict[str, int] = {}
    for r in runs:
        first.setdefault(r.import_batch or f"#{r.id}", r.id)
    runs.sort(key=lambda r: (-first[r.import_batch or f"#{r.id}"], r.id))
    counts = _counts(db, [r.id for r in runs])
    return [_tr_out(r, counts[r.id]) for r in runs]


class Selection(BaseModel):
    """Which cases: explicit ids, or a filter on a field, or all active cases of the project."""
    case_ids: list[int] | None = Field(default=None, max_length=5000)
    filter_field: str = ""
    filter_values: list[str] = Field(default_factory=list, max_length=500)


class TestRunIn(Selection):
    __test__ = False
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=5000)
    plan: RunPlan = Field(default_factory=lambda: RunPlan(mode="single"))
    confirm: bool = True


def _select_cases(db: Session, pid: int, sel: Selection, tenant_id: int) -> list[TestCase]:
    q = select(TestCase).where(TestCase.tenant_id == tenant_id, TestCase.project_id == pid, TestCase.archived.is_(False)).order_by(TestCase.id)
    if sel.case_ids is not None:
        q = q.where(TestCase.id.in_(sel.case_ids))
    cases = list(db.scalars(q).all())
    if sel.filter_field:
        wanted = set(sel.filter_values)
        cases = [c for c in cases if im.group_value({k: getattr(c, k, "") for k in im.FIELD if hasattr(c, k)},
                                                    c.attributes or {}, sel.filter_field) in wanted]
    return cases


@router.post("/projects/{pid}/test-runs/plan")
def plan_test_runs(pid: int, body: TestRunIn, db: Session = Depends(get_db), me: User = Depends(member)):
    """Preview: which groups (and how many cases each) a grouping of existing cases would give."""
    _project(db, pid, me, "run.execute")
    cases = _select_cases(db, pid, body, me.tenant_id)
    items = [({k: getattr(c, k, "") for k in im.FIELD if hasattr(c, k)}, c.attributes or {}) for c in cases]
    custom = sorted({k for c in cases for k in (c.attributes or {}) if k not in {f.label for f in im.FIELDS}})
    recs = [im.Record(0, s, a, {}) for s, a in items]
    opts = im.group_options(recs, custom)
    return {"total": len(cases), "group_options": opts,
            "groups": im.groups_for(items, body.plan.group_by) if body.plan.group_by else [],
            "recommended_group_by": im.recommend_grouping(opts, len(cases))}


@router.post("/projects/{pid}/test-runs", status_code=201)
def create_test_runs(pid: int, body: TestRunIn, request: Request, db: Session = Depends(get_db),
                     me: User = Depends(member)):
    _project(db, pid, me, "run.execute")
    if not body.confirm:
        raise HTTPException(422, "Confirm to create the test run(s)")
    cases = _select_cases(db, pid, body, me.tenant_id)
    if not cases:
        raise HTTPException(422, "No test cases match the selection")
    plan = body.plan.model_copy(update={"name": body.name or body.plan.name, "apply_results": False})
    if plan.mode == "none":
        plan.mode = "single"
    if plan.mode == "single" and not plan.name.strip():
        raise HTTPException(422, "Give the test run a name")
    runs = _create_runs(db, pid, me, plan, [(c, None) for c in cases], "manual", uuid.uuid4().hex[:12], "")
    if len(runs) == 1 and body.description:
        runs[0].description = body.description
    audit(db, request, me, "test_runs_created", str(len(runs)), project_id=pid, count=len(runs), cases=len(cases))
    db.commit()
    counts = _counts(db, [r.id for r in runs])
    return [_tr_out(r, counts[r.id]) for r in runs]


def _item_out(it: TestRunItem, auto_status: str | None = None) -> dict:
    c = it.case
    return {"id": it.id, "test_case_id": c.id, "position": it.position, "status": it.status,
            "actual_result": it.actual_result, "notes": it.notes, "assigned_to": it.assigned_to or c.assigned_to,
            "executed_by": it.executed_by_name, "executed_at": it.executed_at,
            "automated_run_id": it.automated_run_id, "automated_status": auto_status,
            "case": {"external_id": c.external_id, "name": c.name, "module": c.module, "sub_module": c.sub_module,
                     "priority": c.priority, "scenario_type": c.scenario_type, "preconditions": c.preconditions,
                     "test_data": c.test_data, "procedure": c.procedure, "expected_result": c.expected_result,
                     "description": c.description, "has_steps": bool(c.steps), "archived": c.archived}}


@router.get("/test-runs/{tid}")
def get_test_run(tid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    tr = _get_tr(db, tid, me)
    items = db.scalars(select(TestRunItem).where(TestRunItem.tenant_id == me.tenant_id, TestRunItem.test_run_id == tid)
                       .options(selectinload(TestRunItem.case)).order_by(TestRunItem.position, TestRunItem.id)).all()
    auto_ids = [i.automated_run_id for i in items if i.automated_run_id]
    auto = dict(db.execute(select(Run.id, Run.status).where(Run.tenant_id == me.tenant_id,
                                                                    Run.id.in_(auto_ids))).all()) if auto_ids else {}
    counts = _counts(db, [tid])[tid]
    creator = db.get(User, tr.created_by) if tr.created_by else None
    out = _tr_out(tr, counts, creator.display_name or creator.username if creator else None)
    out["items"] = [_item_out(i, auto.get(i.automated_run_id)) for i in items]
    return out


class TestRunPatch(BaseModel):
    __test__ = False
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "closed"] | None = None


@router.patch("/test-runs/{tid}")
def update_test_run(tid: int, body: TestRunPatch, request: Request, db: Session = Depends(get_db),
                    me: User = Depends(member)):
    tr = _get_tr(db, tid, me, "run.execute")
    if body.name is not None:
        tr.name = body.name
    if body.description is not None:
        tr.description = body.description
    if body.status and body.status != tr.status:
        tr.status = body.status
        tr.closed_at = utcnow() if body.status == "closed" else None
    audit(db, request, me, "test_run_updated", tr.name, test_run_id=tid)
    db.commit()
    return _tr_out(tr, _counts(db, [tid])[tid])


@router.delete("/test-runs/{tid}")
def delete_test_run(tid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    """Deletes the test run and its recorded results. Test cases and automated runs are kept."""
    tr = _get_tr(db, tid, me, "run.execute")
    audit(db, request, me, "test_run_deleted", tr.name, test_run_id=tid)
    db.delete(tr)
    db.commit()
    return {"ok": True}


class AddItems(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=5000)


@router.post("/test-runs/{tid}/items")
def add_items(tid: int, body: AddItems, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    tr = _get_tr(db, tid, me, "run.execute")
    have = set(db.scalars(select(TestRunItem.test_case_id).where(TestRunItem.test_run_id == tid)).all())
    pos = (db.scalar(select(func.max(TestRunItem.position)).where(TestRunItem.test_run_id == tid)) or 0) + 1
    cases = db.scalars(select(TestCase).where(TestCase.tenant_id == me.tenant_id, TestCase.project_id == tr.project_id,
                                              TestCase.id.in_(body.case_ids),
                                              TestCase.archived.is_(False)).order_by(TestCase.id)).all()
    added = 0
    for c in cases:
        if c.id in have:
            continue
        db.add(TestRunItem(tenant_id=me.tenant_id, test_run_id=tid, test_case_id=c.id, position=pos, assigned_to=c.assigned_to or ""))
        pos += 1
        added += 1
    audit(db, request, me, "test_run_items_added", tr.name, test_run_id=tid, added=added)
    db.commit()
    return {"added": added}


@router.delete("/test-runs/{tid}/items/{iid}")
def remove_item(tid: int, iid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _get_tr(db, tid, me, "run.execute")
    it = db.get(TestRunItem, iid)
    if not it or it.test_run_id != tid or it.tenant_id != me.tenant_id:
        raise HTTPException(404, "Item not found")
    db.delete(it)
    audit(db, request, me, "test_run_item_removed", str(iid), test_run_id=tid)
    db.commit()
    return {"ok": True}


class ItemPatch(BaseModel):
    status: Literal["untested", "passed", "failed", "blocked", "retest", "skipped"] | None = None
    actual_result: str | None = Field(default=None, max_length=20000)
    notes: str | None = Field(default=None, max_length=20000)
    assigned_to: str | None = Field(default=None, max_length=120)


def _record(it: TestRunItem, body: ItemPatch, me: User) -> None:
    if body.status is not None and body.status != it.status:
        it.status = body.status
        if body.status == "untested":
            it.executed_at, it.executed_by, it.executed_by_name = None, None, ""
        else:
            it.executed_at, it.executed_by = utcnow(), me.id
            it.executed_by_name = (me.display_name or me.username)[:120]
    for k in ("actual_result", "notes", "assigned_to"):
        v = getattr(body, k)
        if v is not None:
            setattr(it, k, v)


def _check_open(db: Session, tid: int, me: User) -> TestRun:
    tr = _get_tr(db, tid, me, "run.execute")
    if tr.status == "closed":
        raise HTTPException(409, "This test run is closed. Reopen it to record results.")
    return tr


@router.patch("/test-runs/{tid}/items/{iid}")
def update_item(tid: int, iid: int, body: ItemPatch, db: Session = Depends(get_db), me: User = Depends(member)):
    _check_open(db, tid, me)
    it = db.get(TestRunItem, iid)
    if not it or it.test_run_id != tid or it.tenant_id != me.tenant_id:
        raise HTTPException(404, "Item not found")
    _record(it, body, me)
    db.commit()
    return _item_out(it)


class BulkItems(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=5000)
    status: Literal["untested", "passed", "failed", "blocked", "retest", "skipped"]


@router.post("/test-runs/{tid}/items/bulk")
def bulk_items(tid: int, body: BulkItems, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _check_open(db, tid, me)
    items = db.scalars(select(TestRunItem).where(TestRunItem.tenant_id == me.tenant_id, TestRunItem.test_run_id == tid,
                                                 TestRunItem.id.in_(body.item_ids))).all()
    for it in items:
        _record(it, ItemPatch(status=body.status), me)
    audit(db, request, me, "test_run_bulk_result", str(tid), test_run_id=tid, status=body.status, count=len(items))
    db.commit()
    return {"updated": len(items)}


class AutomateIn(BaseModel):
    item_ids: list[int] | None = None


@router.post("/test-runs/{tid}/automate")
def automate(tid: int, request: Request, body: AutomateIn | None = None, db: Session = Depends(get_db),
             me: User = Depends(member)):
    """Queues an automated browser run for every selected item whose case has automated steps.
    The worker writes each run's outcome back into the item."""
    tr = _check_open(db, tid, me)
    q = select(TestRunItem).where(TestRunItem.tenant_id == me.tenant_id, TestRunItem.test_run_id == tid).options(selectinload(TestRunItem.case))
    if body and body.item_ids:
        q = q.where(TestRunItem.id.in_(body.item_ids))
    items = db.scalars(q).all()
    queued, manual, problems = 0, 0, []
    for it in items:
        if not it.case.steps or it.case.archived:
            manual += 1
            continue
        try:
            r = queue_case_run(db, it.case, me)
        except HTTPException as e:
            problems.append(f"{it.case.external_id or it.case.name}: {e.detail}")
            if e.status_code in (403, 409) or "Cannot run" in str(e.detail):
                break  # project- or organization-level problem: the same for every case
            continue
        it.automated_run_id = r.id
        queued += 1
    audit(db, request, me, "test_run_automated", tr.name, test_run_id=tid, queued=queued)
    db.commit()
    return {"queued": queued, "manual": manual, "problems": problems[:20]}


@router.get("/test-runs/{tid}/export")
def export_test_run(tid: int, format: str = "xlsx", db: Session = Depends(get_db), me: User = Depends(member)):
    tr = _get_tr(db, tid, me)
    items = db.scalars(select(TestRunItem).where(TestRunItem.tenant_id == me.tenant_id, TestRunItem.test_run_id == tid)
                       .options(selectinload(TestRunItem.case)).order_by(TestRunItem.position, TestRunItem.id)).all()
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", tr.name).strip("-")[:60] or "test-run"
    if format == "csv":
        body, mime = im.export_csv(items), "text/csv; charset=utf-8"
    elif format == "xlsx":
        body, mime = im.export_xlsx(items, tr.name), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise HTTPException(422, "format must be xlsx or csv")
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="testrun-{tr.id}-{name}.{format}"',
        "Cache-Control": "private, no-store"})

# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/models.py
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""Shared pydantic models for curate-skills scripts.

Imported by the other scripts (no CLI). Each importing script declares
`pydantic` in its own PEP 723 header.
"""
from __future__ import annotations

import sys
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def clean_dependencies(deps) -> list[str]:
    """Keep only real third-party PyPI packages.

    Drops the FaaSr runtime (faasr / FaaSr_py — injected, never pip-installed) and
    any standard-library module (re, json, os, collections, …) which isn't on PyPI.
    Dedupes, preserving order.
    """
    _runtime = {"faasr", "faasr_py", "faasrpy"}
    _stdlib = set(sys.stdlib_module_names)
    out: list[str] = []
    for raw in deps or []:
        name = str(raw).strip()
        if not name:
            continue
        base = name.lower().replace("-", "_")
        if base in _runtime or base in _stdlib:
            continue
        if name not in out:
            out.append(name)
    return out


class IOSpec(BaseModel):
    name: str
    type: str = "any"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _flatten_name(cls, v: str) -> str:
        """Filenames on the data store are flat — the S3 folder is a separate
        FaaSr argument"""
        return v.rsplit("/", 1)[-1].strip()


class FunctionSpec(BaseModel):
    name: str
    description: str = ""
    inputs: list[IOSpec] = Field(default_factory=list)
    outputs: list[IOSpec] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)  # PyPI package names
    secrets: list[str] = Field(default_factory=list)  # uppercase env-var names, read via faasr_secret()
    rank: int = 1  # >1 → ranked/parallel: invoked as Name(N), each instance gets faasr_rank()
    # "user_provided": seeded from a user-attached script/function; the MCA decides
    # user_model_mode (verbatim wrap vs adapt), the MGA executes it and verifies parity.
    source: Literal["catalog", "adapt", "new", "cached", "user_provided"] = "new"
    # Only for source == "user_provided". "verbatim": the model's code must not change —
    # it is copied unchanged and wrapped with FaaSr I/O + an entry function that CALLS
    # the model. "adapt": the model's own computation must change per the user's request.
    user_model_mode: Optional[Literal["verbatim", "adapt"]] = None
    catalog_id: Optional[str] = None
    code: Optional[str] = None  # Python function source
    tests: Optional[str] = None  # Python test source
    # Provenance for reused/adapted/cached nodes: where the code came from.
    # "catalog" → seeded/reused from a catalog function; "cache" → this workflow's
    # own genuinely-implemented prior code. origin_name is the source function name.
    origin_kind: Optional[Literal["catalog", "cache"]] = None
    origin_name: Optional[str] = None


class UserModel(BaseModel):
    """A user-attached Python script or function, brought in as a workflow node."""
    name: str  # snake_case node name (derived from the filename stem)
    path: str  # absolute source path
    description: str = ""


class ContextFile(BaseModel):
    """A user-attached non-code reference file (paper, I/O example, dataset, doc)."""
    name: str  # basename, e.g. "adm1_paper.pdf"
    path: str  # absolute source path
    description: str = ""


class DataFlowEdge(BaseModel):
    from_node: str
    to_node: str
    file: str  # concrete filename on the data store
    format: str = ""  # e.g. "csv", "json", "png"
    folder: str = "data"


class WorkflowEdge(BaseModel):
    from_node: str
    to_node: str
    condition: Optional[Literal["True", "False"]] = None  # for conditional branching


class WorkflowSpec(BaseModel):
    name: str
    description: str = ""
    nodes: list[FunctionSpec] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    data_flow: list[DataFlowEdge] = Field(default_factory=list)
    entry: str = ""  # name of the entry-point function
    compute_target: str = "GH"  # key into ComputeServers
    data_store: str = "S3"  # key into DataStores
    folder: str = "workflow_data"  # S3 folder prefix


# Run-state / provenance fields that describe how a particular run sourced a
# function. Always re-derived on reuse, so they are excluded from catalog and
# workflow entries on disk.
CATALOG_EXCLUDED_SPEC_FIELDS = {"tests", "origin_kind", "origin_name", "catalog_id"}


class CatalogEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    function_spec: FunctionSpec
    keywords: list[str] = Field(default_factory=list)
    provenance: str = ""  # e.g. "workflow:<name>"
    usage_count: int = 0


class WorkflowEntry(BaseModel):
    """A stored, reusable complete workflow (analogous to CatalogEntry)."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_spec: WorkflowSpec
    keywords: list[str] = Field(default_factory=list)
    provenance: str = ""  # e.g. "user request"
    usage_count: int = 0

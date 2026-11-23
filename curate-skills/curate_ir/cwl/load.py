# /// script
# requires-python = ">=3.11"
# dependencies = ["cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""Load a CURATE workflow (CWL v1.2, docs/CWL_IR.md) into one plain structure.

  from cwl.load import load
  wf = load("workflow.yml", inputs="inputs.yml")     # any file extension

`cwl-utils` parses the document (it checks it against the CWL v1.2 schema and resolves the
short forms: maps or lists, `in: {x: y}` or `source:`). The result is turned into the plain
dataclasses below, with ids relative to the workflow ("rank3", "map/output1"), types as strings
("File", "File[]", "int[]", "File[][]") and the curate: extensions read into fields. Values from
the optional inputs file replace the workflow's defaults.

The loader does not judge the profile; curate_ir/cwl/validate.py does (it checks the raw
document first, so refused constructs get their own message instead of a parse error).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from cwl import NS
except ImportError:  # run as a script from this folder
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cwl import NS

SCHEMA_ORG = "https://schema.org/"
NO_VALUE = object()  # a workflow input with neither a default nor a value in the inputs file


class LoadError(Exception):
    """The file is not readable YAML, or not a CWL v1.2 document cwl-utils can parse."""


@dataclass
class Port:
    """An Operation input or output."""
    type: str
    axis: str | None = None  # curate:axis on a File[] output


@dataclass
class In:
    """A step's `in:` entry."""
    sources: list[str] = field(default_factory=list)
    default: Any = NO_VALUE
    link_merge: str | None = None
    folder: bool = False  # curate:folder


@dataclass
class Hint:
    cls: str            # "DockerRequirement", "curate:function", ...
    fields: dict
    where: str          # "hints" or "requirements"


@dataclass
class Step:
    id: str
    ins: dict[str, In]
    outs: list[str]
    op_inputs: dict[str, Port]
    op_outputs: dict[str, Port]
    scatter: list[str] = field(default_factory=list)
    scatter_method: str | None = None
    hints: list[Hint] = field(default_factory=list)  # step hints and requirements

    def hint(self, cls: str) -> dict | None:
        return next((h.fields for h in self.hints if h.cls == cls), None)

    @property
    def function(self) -> dict | None:
        return self.hint("curate:function")

    @property
    def image(self) -> str | None:
        h = self.hint("DockerRequirement")
        return h.get("dockerPull") if h else None

    @property
    def packages(self) -> list[str]:
        h = self.hint("SoftwareRequirement") or {}
        return [p.get("package") if isinstance(p, dict) else str(p) for p in h.get("packages") or []]

    @property
    def resources(self) -> dict:
        h = self.hint("ResourceRequirement") or {}
        return {k: v for k, v in h.items() if v is not None}

    @property
    def after(self) -> list[str]:
        return list((self.hint("curate:after") or {}).get("steps") or [])

    @property
    def run_if(self) -> dict | None:
        return self.hint("curate:run_if")

    @property
    def secrets(self) -> list[str]:
        return list((self.hint("curate:secrets") or {}).get("names") or [])


@dataclass
class WfInput:
    type: str
    default: Any = NO_VALUE
    value: Any = NO_VALUE  # the inputs file's value, else the default


@dataclass
class WfOutput:
    type: str
    sources: list[str]
    deliver_as: str | None = None


@dataclass
class Workflow:
    path: Path
    base: Path
    inputs: dict[str, WfInput]
    outputs: dict[str, WfOutput]
    steps: dict[str, Step]
    label: str | None = None
    doc: str | None = None
    identifier: str | None = None
    version: str | None = None
    content_hash: str | None = None
    hints: list[Hint] = field(default_factory=list)  # workflow hints and requirements
    inputs_file: Path | None = None
    unknown_inputs: list[str] = field(default_factory=list)  # keys of the inputs file with no workflow input

    def hint(self, cls: str) -> dict | None:
        return next((h.fields for h in self.hints if h.cls == cls), None)

    @property
    def image(self) -> str | None:
        h = self.hint("DockerRequirement")
        return h.get("dockerPull") if h else None

    @property
    def resources(self) -> dict:
        h = self.hint("ResourceRequirement") or {}
        return {k: v for k, v in h.items() if v is not None}


# ── normalising cwl-utils objects ────────────────────────────────────────────

def frag(uri: str) -> str:
    """'file:///x/wf.yml#map/output1' -> 'map/output1'."""
    return str(uri).split("#", 1)[-1]


def last(uri: str) -> str:
    return frag(uri).rsplit("/", 1)[-1]


def type_str(t) -> str:
    """A cwl-utils type as a string: 'File', 'File[]', 'int[][]'; a union joins with '|'."""
    if isinstance(t, str):
        return t.rsplit("#", 1)[-1]
    if isinstance(t, list):
        return "|".join(type_str(x) for x in t)
    items = getattr(t, "items", None)
    if items is not None:
        return type_str(items) + "[]"
    kind = getattr(t, "type_", None)
    return type_str(kind) if kind is not None else type(t).__name__


def class_name(cls: str) -> str:
    cls = str(cls)
    if cls.startswith(NS):
        return "curate:" + cls[len(NS):]
    if cls.startswith("curate:"):
        return cls
    return cls.rsplit("#", 1)[-1]


def _ext(obj, name: str, default=None):
    ext = getattr(obj, "extension_fields", None) or {}
    for key in (NS + name, "curate:" + name):
        if key in ext:
            return ext[key]
    return default


_SKIP = {"class_", "extension_fields", "loadingOptions"}


def hint_of(h, where: str) -> Hint:
    """A hint or requirement, which cwl-utils gives as a dict (unknown classes) or an object."""
    if isinstance(h, dict):
        fields = {k: v for k, v in h.items() if k != "class"}
        return Hint(class_name(h.get("class", "")), fields, where)
    fields = {k: v for k, v in vars(h).items() if k not in _SKIP and not k.startswith("_")}
    for k, v in list(fields.items()):  # SoftwareRequirement packages are objects too
        if isinstance(v, list):
            fields[k] = [({kk: vv for kk, vv in vars(x).items() if kk not in _SKIP and vv is not None}
                          if hasattr(x, "__dict__") else x) for x in v]
    return Hint(class_name(getattr(h, "class_", type(h).__name__)), fields, where)


def hints_of(obj) -> list[Hint]:
    return ([hint_of(h, "requirements") for h in getattr(obj, "requirements", None) or []]
            + [hint_of(h, "hints") for h in getattr(obj, "hints", None) or []])


def _resolve_files(value, base: Path):
    """File/Directory values with a relative location or path get an absolute `path`."""
    if isinstance(value, list):
        return [_resolve_files(v, base) for v in value]
    if isinstance(value, dict) and value.get("class") in ("File", "Directory"):
        out = dict(value)
        loc = out.get("path") or out.get("location") or ""
        if loc and "://" not in loc:
            out["path"] = str((base / loc).resolve()) if not loc.startswith("/") else loc
        elif loc.startswith("file://"):
            out["path"] = loc[len("file://"):]
        return out
    return value


def read_yaml(path: Path):
    try:
        return yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise LoadError(f"{path}: cannot read: {_one_line(e)}") from e


def _one_line(e: Exception) -> str:
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    text = " ".join(lines)
    text = re.sub(r"\(\d+\) \(file://[^)]*\) ", "", text)
    return text[:400] + ("…" if len(text) > 400 else "")


def load(path, inputs=None) -> Workflow:
    """Parse `path` (any extension) with cwl-utils, plus the optional inputs file."""
    from cwl_utils.parser import load_document_by_string

    path = Path(path).resolve()
    try:
        text = path.read_text()
    except OSError as e:
        raise LoadError(f"{path}: cannot read: {e}") from e
    try:
        doc = load_document_by_string(text, path.as_uri())
    except Exception as e:  # schema_salad raises several exception types
        raise LoadError(f"not valid CWL v1.2: {_one_line(e)}") from e
    if type(doc).__name__ != "Workflow":
        raise LoadError(f"the document is a {type(doc).__name__}, not a Workflow")
    base = path.parent

    wf_inputs = {}
    for i in doc.inputs:
        d = NO_VALUE if i.default is None else _resolve_files(i.default, base)
        wf_inputs[frag(i.id)] = WfInput(type_str(i.type_), d, d)
    wf_outputs = {}
    for o in doc.outputs:
        src = o.outputSource
        srcs = [frag(s) for s in (src if isinstance(src, list) else [src] if src else [])]
        wf_outputs[frag(o.id)] = WfOutput(type_str(o.type_), srcs, _ext(o, "deliver_as"))

    steps = {}
    for s in doc.steps:
        sid = frag(s.id)
        run = s.run
        ins = {}
        for e in s.in_:
            src = e.source
            srcs = [frag(x) for x in (src if isinstance(src, list) else [src] if src else [])]
            ins[last(e.id)] = In(srcs, NO_VALUE if e.default is None else _resolve_files(e.default, base),
                                 e.linkMerge, bool(_ext(e, "folder", False)))
        op_in = {last(p.id): Port(type_str(p.type_)) for p in getattr(run, "inputs", None) or []}
        op_out = {last(p.id): Port(type_str(p.type_), _ext(p, "axis"))
                  for p in getattr(run, "outputs", None) or []}
        scatter = s.scatter if isinstance(s.scatter, list) else [s.scatter] if s.scatter else []
        steps[sid] = Step(sid, ins, [last(o if isinstance(o, str) else o.id) for o in s.out or []],
                          op_in, op_out, [last(x) for x in scatter], s.scatterMethod,
                          hints_of(s) + [Hint(h.cls, h.fields, "run " + h.where) for h in hints_of(run)])

    ext = doc.extension_fields or {}
    wf = Workflow(path, base, wf_inputs, wf_outputs, steps, label=doc.label, doc=doc.doc,
                  identifier=ext.get(SCHEMA_ORG + "identifier"),
                  version=None if ext.get(SCHEMA_ORG + "version") is None else str(ext[SCHEMA_ORG + "version"]),
                  content_hash=ext.get(NS + "content_hash"), hints=hints_of(doc))

    if inputs is not None:
        ipath = Path(inputs).resolve()
        values = read_yaml(ipath) or {}
        if not isinstance(values, dict):
            raise LoadError(f"{ipath}: an inputs file is a mapping of input names to values")
        wf.inputs_file = ipath
        for k, v in values.items():
            if k in wf.inputs:
                wf.inputs[k].value = _resolve_files(v, ipath.parent)
            else:
                wf.unknown_inputs.append(k)
    return wf

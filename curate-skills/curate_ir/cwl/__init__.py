"""CURATE's CWL IR (docs/CWL_IR.md): CWL v1.2 restricted to a profile, plus `curate:` extensions.

NS is the one place the extension namespace is defined; everything else imports it.
"""
NS = "https://github.com/CURATE-skills/CURATE/blob/main/docs/CWL_IR.md#"
PREFIX = "curate"

# name -> where it may appear
EXTENSIONS = {
    "function": "step hints",
    "deliver_as": "workflow output",
    "folder": "step input",
    "after": "step hints",
    "run_if": "step hints",
    "secrets": "step hints",
    "axis": "Operation output",
}
# Workflow-level metadata fields in the curate namespace (not extensions of the dataflow).
METADATA = ("content_hash",)

"""Every setting a user is meant to configure must be reachable from the UI.

This exists because of a real defect. Approval gates were fully built — the
column, the worker parking on them, the approve/reject card, the dashboard
panel, notification events — and no control anywhere in the app could turn one
on. Every gate that ever existed in development was created by a test helper,
by the seeder or by curl, so the feature looked finished from every angle the
test suite could see.

No behavioural test can catch that. A backend suite proves "given a gated task,
the runtime is correct"; it cannot prove a person is able to *produce* a gated
task. This checks the other half: for each field of each input schema, the
frontend must actually publish it, or say here why it does not.
"""

import re
from pathlib import Path

import pytest

from runrail import schemas

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"

#: Schemas describing things a person configures. Read-only projections
#: (…Out) and pure actions (snooze, approve) are deliberately absent.
CONFIG_SCHEMAS = ("ProjectIn", "EnvironmentIn", "WorkflowIn", "TaskIn", "RunNoteIn")

#: Fields the UI sets through a computed key, which no source scan can see.
#: Each entry must say how it is really published — an unexplained gap is the
#: bug this module exists to catch, so the list stays short and specific.
#: Empty today: the task modals name every path field outright, which they have
#: to anyway now that an omitted field means "leave it alone".
PUBLISHED_INDIRECTLY: set[tuple[str, str]] = set()


def _code_only(source: str) -> str:
    """The source with TypeScript type declarations removed.

    An interface member and a request-body key look identical to a text search
    (`requires_approval: …`), and it was exactly that collision which let the
    missing control look present. Declarations are dropped so only executable
    code is searched.
    """
    out, i = [], 0
    for match in re.finditer(r"\b(?:interface\s+\w+|type\s+\w+\s*=)\s*\{", source):
        if match.start() < i:
            continue
        out.append(source[i:match.start()])
        depth, j = 0, match.end() - 1
        while j < len(source):
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    out.append(source[i:])
    return "".join(out)


@pytest.fixture(scope="module")
def frontend_code() -> str:
    if not FRONTEND.is_dir():
        pytest.skip("frontend sources are not present in this checkout")
    files = [p for p in FRONTEND.rglob("*.ts*") if ".test." not in p.name]
    assert files, "no frontend sources found — the scan would pass vacuously"
    return "\n".join(_code_only(p.read_text(encoding="utf-8")) for p in files)


def _publishes(field: str, code: str) -> bool:
    """Whether the UI can actually send this field.

    Either a form control carries the name, or the key appears in code that
    builds a request body or reads FormData.
    """
    return f'name="{field}"' in code or re.search(rf"\b{re.escape(field)}\s*:", code) is not None


@pytest.mark.parametrize("schema_name", CONFIG_SCHEMAS)
def test_every_configurable_field_has_a_control(schema_name: str, frontend_code: str) -> None:
    model = getattr(schemas, schema_name)
    unreachable = sorted(
        field for field in model.model_fields
        if (schema_name, field) not in PUBLISHED_INDIRECTLY
        and not _publishes(field, frontend_code)
    )
    assert not unreachable, (
        f"{schema_name}: {', '.join(unreachable)} can be stored but not set from the UI. "
        "Add a control, or add it to PUBLISHED_INDIRECTLY with the reason."
    )


def test_the_scan_can_actually_fail() -> None:
    """Guard the guard: a field nothing publishes must be reported."""
    assert not _publishes("a_field_no_control_sets", "const x = { name: 1 };")
    assert _publishes("requires_approval", 'const b = { requires_approval: true };')
    assert _publishes("retries", '<input name="retries" />')


def test_type_declarations_do_not_count_as_controls() -> None:
    """The precise blind spot that hid the missing approval control."""
    declaration_only = _code_only(
        "interface Task {\n  id: number;\n  requires_approval?: boolean;\n}\n"
        "export function render() { return null; }\n"
    )
    assert not _publishes("requires_approval", declaration_only)

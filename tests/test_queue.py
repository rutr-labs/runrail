from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from runrail.db import Base
from runrail.models import TriggerType, Workflow, WorkflowRun
from runrail.worker.queue import claim_next_run


def test_claims_only_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workflow = Workflow(name="queue-test"); db.add(workflow); db.flush()
        db.add(WorkflowRun(workflow_id=workflow.id, trigger_type=TriggerType.manual)); db.commit()
        assert claim_next_run(db).status.value == "running"
        assert claim_next_run(db) is None

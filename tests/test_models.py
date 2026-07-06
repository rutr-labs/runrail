from runrail.models import Task, TaskType, Workflow


def test_model_creation():
    workflow = Workflow(name="daily")
    task = Task(name="extract", task_type=TaskType.shell, command="true", workflow=workflow)
    assert task.workflow.name == "daily"


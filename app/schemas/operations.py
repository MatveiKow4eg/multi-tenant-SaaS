from pydantic import BaseModel


class OperationTaskResponse(BaseModel):
    task_id: str

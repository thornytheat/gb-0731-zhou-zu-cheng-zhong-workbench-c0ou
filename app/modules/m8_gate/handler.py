from fastapi import APIRouter, Request, Response

from app.modules.m8_gate.service import gate_view, issue_permit

router = APIRouter(prefix="/api/batches/{run_id}/gate", tags=["闸口放行"])


@router.get("")
def gate(run_id: str, request: Request):
    return gate_view(request.app.state.store, run_id)


@router.post("/permits/{pass_id}")
def permit(run_id: str, pass_id: str, request: Request, response: Response):
    payload, reused = issue_permit(request.app.state.store, run_id, pass_id)
    response.status_code = 200 if reused else 201
    return {"permit": payload, "reused": reused}

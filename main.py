from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from communityagent.agent.graph import run_agent

STATIC_DIR = Path(__file__).resolve().parent / "communityagent" / "static"

app = FastAPI(title="AI Community Agent")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RunRequest(BaseModel):
    prompt: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/run")
def run(req: RunRequest):
    """Run the agent on a natural-language request and return the full trace
    (reasoning steps, tools selected, API results, final response) so the
    frontend can render the live workflow for judges."""
    result = run_agent(req.prompt)
    return result


@app.get("/api/health")
def health():
    return {"status": "ok"}

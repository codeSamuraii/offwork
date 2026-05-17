"""FastAPI endpoint that offloads PDF rendering to a offwork worker.

Generating a PDF is the textbook "offload" workload: it pulls in a heavy
dependency (``reportlab``), uses a chunk of CPU, and the request handler
just wants bytes back.  The web app stays light; the worker pool absorbs
the load and can be scaled independently.

Only the entry point is decorated with ``@offwork.task``.  The two helpers
(``_styled_table``, ``_build_pdf``) are plain functions; offwork walks
the AST of ``render_report``, sees the calls, and ships their source
along automatically.

Endpoints:
    POST /reports   -- accept JSON, return application/pdf

Usage:
    offwork worker --backend redis://localhost:6379 --tmp
    python examples/pdf_report.py

The script starts the FastAPI app in-process, posts a sample report,
prints the resulting PDF size (and writes it to ``/tmp/offwork_report.pdf``),
and exits.
"""

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

import offwork


# --- helpers (no @offwork.task -- auto-discovered) -------------------------------

def _styled_table(rows: list[list[str | float]]) -> Table:
    table = Table([["Item", "Amount"]] + rows, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
    ]))
    return table


def _build_pdf(title: str, table: Table) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    doc.build([Paragraph(title, styles["Title"]), table])
    return buf.getvalue()


# --- entry point ----------------------------------------------------------

@offwork.task
def render_report(title: str, rows: list[list[str | float]]) -> bytes:
    """Render a titled table to PDF bytes."""
    return _build_pdf(title, _styled_table(rows))


# --- FastAPI app ----------------------------------------------------------

class ReportRequest(BaseModel):
    title: str = "Quarterly Report"
    rows: list[list[str | float]] = [
        ["Revenue", 120000],
        ["Costs", 80000],
        ["Profit", 40000],
    ]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    offwork.connect("local://localhost:9748")
    try:
        yield
    finally:
        await offwork.disconnect()


app = FastAPI(title="offwork PDF service", lifespan=lifespan)


@app.post("/reports")
async def make_report(req: ReportRequest) -> Response:
    pdf = await render_report.run(req.title, req.rows)
    return Response(content=pdf, media_type="application/pdf")


# --- self-driving demo ----------------------------------------------------

async def _demo() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.5)

    payload = {
        "title": "Q1 2026 Report",
        "rows": [
            ["Revenue", 240_000],
            ["Cost of goods", 90_000],
            ["Operating expenses", 55_000],
            ["Net profit", 95_000],
        ],
    }
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080") as client:
            resp = await client.post("/reports", json=payload, timeout=60.0)
            resp.raise_for_status()
            out = Path("/tmp/offwork_report.pdf")
            out.write_bytes(resp.content)
            print(f"PDF: {len(resp.content)} bytes -> {out}")
    finally:
        server.should_exit = True
        await task


if __name__ == "__main__":
    asyncio.run(_demo())

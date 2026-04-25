"""FastAPI endpoint that offloads PDF rendering to a pyfuse worker.

Generating a PDF is the textbook "offload" workload: it pulls in a heavy
dependency (``reportlab``), uses a chunk of CPU, and the request handler
just wants bytes back.  The web app stays light; the worker pool absorbs
the load and can be scaled independently.

Only the entry point is decorated with ``@trace``.  The two helpers
(``_styled_table``, ``_build_pdf``) are plain functions; pyfuse walks
the AST of ``render_report``, sees the calls, and ships their source
along automatically.

Endpoints:
    POST /reports   -- accept JSON, return application/pdf

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    uvicorn examples.pdf_report:app --reload

    curl -X POST localhost:8000/reports -o report.pdf \\
         -H 'content-type: application/json' \\
         -d '{"title":"Q1","rows":[["Revenue",120000],["Costs",80000]]}'
"""

from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

import pyfuse
from pyfuse import trace


# --- helpers (no @trace -- auto-discovered) -------------------------------

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

@trace
def render_report(title: str, rows: list[list[str | float]]) -> bytes:
    """Render a titled table to PDF bytes."""
    return _build_pdf(title, _styled_table(rows))


# --- FastAPI app ----------------------------------------------------------

class ReportRequest(BaseModel):
    title: str
    rows: list[list[str | float]]


app = FastAPI(title="pyfuse PDF service")


@app.on_event("startup")
async def _startup() -> None:
    pyfuse.connect("redis://localhost:6379")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await pyfuse.disconnect()


@app.post("/reports")
async def make_report(req: ReportRequest) -> Response:
    pdf = await render_report.run(req.title, req.rows)
    return Response(content=pdf, media_type="application/pdf")

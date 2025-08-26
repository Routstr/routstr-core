import html
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..core import get_logger
from ..payment.models import MODELS
from ..payment.price import sats_usd_ask_price

logger = get_logger(__name__)

pricing_router = APIRouter()


class EstimateRequest(BaseModel):
    model: str
    prompt_tokens: int
    max_tokens: int


def _provider_from_model_id(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "other"


def _format_sats(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


async def _get_sats_max_cost(model_obj: Any) -> float:
    if (
        getattr(model_obj, "sats_pricing", None) is not None
        and getattr(model_obj.sats_pricing, "max_cost", None) is not None
    ):
        return float(model_obj.sats_pricing.max_cost)

    p_sat, c_sat, req_sat = await _get_sats_pricing_fields(model_obj)

    tp = getattr(model_obj, "top_provider", None)
    if tp and (
        getattr(tp, "context_length", None)
        or getattr(tp, "max_completion_tokens", None)
    ):
        cl = getattr(tp, "context_length", None)
        mct = getattr(tp, "max_completion_tokens", None)
        if cl and mct:
            return (cl - mct) * p_sat + mct * c_sat
        if cl:
            return cl * 0.8 * p_sat + cl * 0.2 * c_sat
        if mct:
            return mct * 4 * p_sat + mct * c_sat
        return 1_000_000 * p_sat + 32_000 * c_sat + 100_000 * req_sat

    if getattr(model_obj, "context_length", None):
        cl2 = int(model_obj.context_length)
        return cl2 * 0.8 * p_sat + cl2 * 0.2 * c_sat

    return 1_000_000 * p_sat + 32_000 * c_sat + 100_000 * req_sat


async def _get_sats_pricing_fields(model_obj: Any) -> tuple[float, float, float]:
    if model_obj.sats_pricing is not None:
        return (
            float(model_obj.sats_pricing.prompt),
            float(model_obj.sats_pricing.completion),
            float(getattr(model_obj.sats_pricing, "request", 0.0) or 0.0),
        )

    rate_usd_per_sat = await sats_usd_ask_price()
    return (
        float(model_obj.pricing.prompt) / rate_usd_per_sat,
        float(model_obj.pricing.completion) / rate_usd_per_sat,
        float(getattr(model_obj.pricing, "request", 0.0) or 0.0) / rate_usd_per_sat,
    )


def _render_layout(body: str) -> str:
    return f"""<!DOCTYPE html>
    <html>
        <head>
            <meta charset=\"utf-8\" />
            <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
            <title>Pricing</title>
            <script src=\"https://unpkg.com/htmx.org@1.9.12\"></script>
            <style>
                * {{ box-sizing: border-box; }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fa; color: #2c3e50; line-height: 1.6; padding: 2rem; }}
                h1, h2 {{ margin-bottom: 1rem; color: #1a202c; }}
                .card {{ background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
                .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 1.25rem; }}
                .controls {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin-bottom: 1rem; }}
                input[type=\"text\"], select, input[type=\"number\"] {{ width: 100%; padding: 10px; border: 2px solid #e2e8f0; border-radius: 8px; font-size: 14px; }}
                input:focus, select:focus {{ outline: none; border-color: #4299e1; }}
                table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
                th {{ background: #4a5568; color: white; font-weight: 600; padding: 10px; text-align: left; white-space: nowrap; }}
                td {{ padding: 10px; border-bottom: 1px solid #edf2f7; vertical-align: top; }}
                tr:hover {{ background: #f7fafc; }}
                .muted {{ color: #718096; font-size: 12px; }}
                .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #edf2f7; color: #2d3748; }}
                .btn {{ padding: 10px 14px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; background: #4299e1; color: white; }}
                .btn:hover {{ background: #3182ce; }}
                .right {{ text-align: right; font-family: 'Monaco', monospace; }}
                @media (max-width: 980px) {{ .grid {{ grid-template-columns: 1fr; }} .controls {{ grid-template-columns: 1fr 1fr; }} }}
            </style>
        </head>
        <body>
            {body}
        </body>
    </html>
    """


@pricing_router.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
async def pricing_page() -> str:
    providers: list[str] = sorted({_provider_from_model_id(m.id) for m in MODELS})

    provider_options = ['<option value="">All providers</option>'] + [
        f'<option value="{html.escape(p)}">{html.escape(p)}</option>' for p in providers
    ]

    body = f"""
    <h1>Pricing Transparency</h1>
    <div class=\"grid\">
      <div class=\"card\">
        <h2 style=\"margin-top:0\">Public Pricing</h2>
        <div id=\"filters\" class=\"controls\">
            <input id=\"q\" name=\"q\" type=\"text\" placeholder=\"Filter by model name or id...\"
                hx-get=\"/pricing/table\" hx-trigger=\"keyup changed delay:300ms\"
                hx-target=\"#pricing-table\" hx-swap=\"innerHTML\" hx-include=\"#filters *\" />
            <select id=\"provider\" name=\"provider\"
                hx-get=\"/pricing/table\" hx-trigger=\"change\" hx-target=\"#pricing-table\" hx-include=\"#filters *\">
                {"".join(provider_options)}
            </select>
            <select id=\"sort\" name=\"sort\"
                hx-get=\"/pricing/table\" hx-trigger=\"change\" hx-target=\"#pricing-table\" hx-include=\"#filters *\">
                <option value=\"input_asc\">Cheapest input</option>
                <option value=\"input_desc\">Most expensive input</option>
                <option value=\"output_asc\">Cheapest output</option>
                <option value=\"output_desc\">Most expensive output</option>
                <option value="max_asc">Cheapest max/request</option>
                <option value="max_desc">Most expensive max/request</option>
                <option value="name">Name</option>
            </select>
            <input id=\"max_input\" name=\"max_input\" type=\"number\" min=\"0\" placeholder=\"Max input sats/1K\"
                hx-get=\"/pricing/table\" hx-trigger=\"change keyup delay:300ms\" hx-target=\"#pricing-table\" hx-include=\"#filters *\" />
            <input id="max_request" name="max_request" type="number" min="0" placeholder="Max /request sats"
                hx-get="/pricing/table" hx-trigger="change keyup delay:300ms" hx-target="#pricing-rows" hx-include="#filters *" />
        </div>
        <div id=\"pricing-table\" class=\"card\" style=\"padding:0; overflow:hidden\">
            <table>
                <thead>
                    <tr>
                        <th>Model</th>
                        <th>Input (sats/1K)</th>
                        <th>Output (sats/1K)</th>
                        <th>Max/request (sats)</th>
                    </tr>
                </thead>
                <tbody id="pricing-rows"
                    hx-get="/pricing/table"
                    hx-trigger="load, every 10s"
                    hx-include="#filters *"
                    hx-swap="innerHTML">
                    <tr><td colspan="4" class="muted">Loading…</td></tr>
                </tbody>
            </table>
        </div>
      </div>
      <div class=\"card\">
        <h2 style=\"margin-top:0\">Cost Estimator</h2>
        <form id=\"estimate-form\" hx-post=\"/pricing/estimate\" hx-target=\"#estimate-result\" hx-swap=\"innerHTML\">
            <label class=\"muted\">Model</label>
            <select name=\"model\">{"".join([f'<option value="{html.escape(m.id)}">{html.escape(m.name)} ({html.escape(m.id)})</option>' for m in MODELS])}</select>
            <div style=\"display:grid; grid-template-columns:1fr 1fr; gap:.75rem;\">
                <div>
                    <label class=\"muted\">Prompt tokens</label>
                    <input type=\"number\" name=\"prompt_tokens\" min=\"0\" value=\"500\" />
                </div>
                <div>
                    <label class=\"muted\">Max completion tokens</label>
                    <input type=\"number\" name=\"max_tokens\" min=\"0\" value=\"200\" />
                </div>
            </div>
            <button class=\"btn\" type=\"submit\">Estimate</button>
        </form>
        <div id=\"estimate-result\" class=\"card\" style=\"margin-top:1rem\"></div>
        <div class=\"muted\" style=\"margin-top:0.75rem\">API: POST <code>/v1/estimate</code></div>
      </div>
    </div>
    """

    return _render_layout(body)


@pricing_router.get(
    "/pricing/table", response_class=HTMLResponse, include_in_schema=False
)
async def pricing_table(
    q: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    sort: str = Query(default="input_asc"),
    max_input: str | None = Query(default=None),
    max_request: str | None = Query(default=None),
) -> str:
    rows: list[tuple[str, str, int, int, int]] = []
    max_input_limit: int | None = None
    max_request_limit: int | None = None
    if max_input is not None and str(max_input).strip() != "":
        try:
            max_input_limit = int(str(max_input))
        except ValueError:
            max_input_limit = None
    if max_request is not None and str(max_request).strip() != "":
        try:
            max_request_limit = int(str(max_request))
        except ValueError:
            max_request_limit = None

    for model in MODELS:
        input_sat_per_token, output_sat_per_token, _ = await _get_sats_pricing_fields(
            model
        )
        input_per_k = int(round(input_sat_per_token * 1000))
        output_per_k = int(round(output_sat_per_token * 1000))
        max_cost = int(round(await _get_sats_max_cost(model)))

        if q:
            needle = q.lower().strip()
            if needle and (
                needle not in model.name.lower() and needle not in model.id.lower()
            ):
                continue

        if provider:
            if _provider_from_model_id(model.id) != provider:
                continue

        if max_input_limit is not None and input_per_k > max_input_limit:
            continue
        if max_request_limit is not None and max_cost > max_request_limit:
            continue

        rows.append((model.name, model.id, input_per_k, output_per_k, max_cost))

    if sort == "input_asc":
        rows.sort(key=lambda r: r[2])
    elif sort == "input_desc":
        rows.sort(key=lambda r: r[2], reverse=True)
    elif sort == "output_asc":
        rows.sort(key=lambda r: r[3])
    elif sort == "output_desc":
        rows.sort(key=lambda r: r[3], reverse=True)
    elif sort == "max_asc":
        rows.sort(key=lambda r: r[4])
    elif sort == "max_desc":
        rows.sort(key=lambda r: r[4], reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))

    table_html = []

    for name, mid, inp, outp, mxc in rows:
        cell_model = f'{html.escape(name)}<div class="muted">{html.escape(mid)}</div>'
        table_html.append(
            "  <tr>"
            f"    <td>{cell_model}</td>"
            f'    <td class="right">{_format_sats(inp)}</td>'
            f'    <td class="right">{_format_sats(outp)}</td>'
            f'    <td class="right">{_format_sats(mxc)}</td>'
            "  </tr>"
        )

    return "\n".join(table_html)


async def _estimate_cost_sats(
    model_id: str, prompt_tokens: int, max_tokens: int
) -> tuple[int, int, int, int]:
    model_obj = next(
        (m for m in MODELS if m.id == model_id or m.name == model_id), None
    )
    if model_obj is None:
        raise HTTPException(status_code=404, detail="Model not found")

    p_sat, c_sat, request_sat = await _get_sats_pricing_fields(model_obj)
    prompt_cost = int(round(max(0, prompt_tokens) * p_sat))
    completion_cost = int(round(max(0, max_tokens) * c_sat))
    fees_cost = int(round(request_sat))
    total = prompt_cost + completion_cost + fees_cost
    return total, prompt_cost, completion_cost, fees_cost


@pricing_router.post("/v1/estimate")
async def estimate_api(payload: EstimateRequest) -> dict[str, object]:
    total, prompt_cost, completion_cost, fees_cost = await _estimate_cost_sats(
        payload.model, payload.prompt_tokens, payload.max_tokens
    )
    return {
        "estimated_cost_sats": total,
        "breakdown": {
            "prompt": prompt_cost,
            "completion": completion_cost,
            "fees": fees_cost,
        },
    }


@pricing_router.post(
    "/pricing/estimate", response_class=HTMLResponse, include_in_schema=False
)
async def estimate_partial(request: Request) -> str:
    form = await request.form()
    model_id = str(form.get("model", ""))
    prompt_tokens = int(str(form.get("prompt_tokens", "0") or 0))
    max_tokens = int(str(form.get("max_tokens", "0") or 0))

    total, prompt_cost, completion_cost, fees_cost = await _estimate_cost_sats(
        model_id, prompt_tokens, max_tokens
    )

    return (
        "<div>"
        f'<div><span class="muted">Estimated total:</span> <strong>{_format_sats(total)}</strong> sats</div>'
        f'<div class="muted" style="margin-top:.5rem">Breakdown — prompt: {_format_sats(prompt_cost)} • completion: {_format_sats(completion_cost)} • fees: {_format_sats(fees_cost)}</div>'
        "</div>"
    )

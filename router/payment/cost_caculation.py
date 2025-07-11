import os

from pydantic import BaseModel

from router.models import MODELS

COST_PER_REQUEST = (
    int(os.environ.get("COST_PER_REQUEST", "1")) * 1000
)  # Convert to msats
COST_PER_1K_INPUT_TOKENS = (
    int(os.environ.get("COST_PER_1K_INPUT_TOKENS", "0")) * 1000
)  # Convert to msats
COST_PER_1K_OUTPUT_TOKENS = (
    int(os.environ.get("COST_PER_1K_OUTPUT_TOKENS", "0")) * 1000
)  # Convert to msats
MODEL_BASED_PRICING = os.environ.get("MODEL_BASED_PRICING", "false").lower() == "true"


class CostData(BaseModel):
    base_msats: int
    input_msats: int
    output_msats: int
    total_msats: int


class MaxCostData(CostData):
    pass


class CostDataError(BaseModel):
    message: str
    code: str


def calculate_cost(
    response_data: dict, max_cost: int
) -> CostData | MaxCostData | CostDataError:
    cost_data = MaxCostData(
        base_msats=max_cost,
        input_msats=0,
        output_msats=0,
        total_msats=max_cost,
    )

    if "usage" not in response_data or response_data["usage"] is None:
        print("No usage data in response, using base cost only")
        return cost_data

    MSATS_PER_1K_INPUT_TOKENS = COST_PER_1K_INPUT_TOKENS
    MSATS_PER_1K_OUTPUT_TOKENS = COST_PER_1K_OUTPUT_TOKENS

    if MODEL_BASED_PRICING and MODELS:
        response_model = response_data.get("model", "")
        if response_model not in [model.id for model in MODELS]:
            return CostDataError(
                message=f"Invalid model in response: {response_model}",
                code="model_not_found",
            )

        model = next(model for model in MODELS if model.id == response_model)
        if model.sats_pricing is None:
            return CostDataError(
                message="Model pricing not defined", code="pricing_not_found"
            )

        MSATS_PER_1K_INPUT_TOKENS = model.sats_pricing.prompt * 1_000_000  # type: ignore
        MSATS_PER_1K_OUTPUT_TOKENS = model.sats_pricing.completion * 1_000_000  # type: ignore

    if not (MSATS_PER_1K_OUTPUT_TOKENS and MSATS_PER_1K_INPUT_TOKENS):
        # If no token pricing is configured, just return base cost
        return cost_data

    input_tokens = response_data.get("usage", {}).get("prompt_tokens", 0)
    output_tokens = response_data.get("usage", {}).get("completion_tokens", 0)

    input_msats = int(round(input_tokens / 1000 * MSATS_PER_1K_INPUT_TOKENS, 0))
    output_msats = int(round(output_tokens / 1000 * MSATS_PER_1K_OUTPUT_TOKENS, 0))
    token_based_cost = int(round(input_msats + output_msats, 0))

    return CostData(
        base_msats=0,
        input_msats=input_msats,
        output_msats=output_msats,
        total_msats=token_based_cost,
    )

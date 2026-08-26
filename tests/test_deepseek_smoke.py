import os
from decimal import Decimal

import pytest

from backend.analysis_agent import DeepSeekAnalysisClient
from backend.common import AgentCallType
from backend.config import get_settings
from backend.schemas import AnalysisFacts, ProductMetrics, StoreMetrics, TrustedAnalysisCandidate


def minimal_trusted_facts() -> AnalysisFacts:
    metrics = ProductMetrics.from_totals(
        impressions=10,
        clicks=1,
        orders=1,
        units=1,
        revenue=Decimal("1.00"),
        refunds=0,
    ).model_copy(update={"product_id": "smoke-product", "product_code": "SMOKE-001"})
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="smoke-store",
            impressions=10,
            clicks=1,
            orders=1,
            units=1,
            revenue=Decimal("1.00"),
            refunds=0,
            ctr=Decimal("0.1000"),
            conversion_rate=Decimal("1.0000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("1.0000"),
        ),
        candidates=[
            TrustedAnalysisCandidate(
                product_id="smoke-product",
                product_code="SMOKE-001",
                anomaly_types=["smoke"],
                metrics=metrics,
                business_impact=Decimal("1.00"),
                evidence=["smoke=1"],
            )
        ],
    )


@pytest.mark.deepseek_smoke
@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_SMOKE") != "1", reason="explicit opt-in required"
)
async def test_explicit_deepseek_flash_schema_smoke() -> None:
    settings = get_settings()
    if settings.deepseek_api_key is None:
        pytest.skip("DeepSeek key is not configured")
    client = DeepSeekAnalysisClient(settings)
    result = await client.request(minimal_trusted_facts(), call_type=AgentCallType.PRIMARY)
    assert result.response is not None
    assert result.records[-1].model == "deepseek-flash"

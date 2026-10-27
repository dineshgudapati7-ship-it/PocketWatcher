from pydantic import BaseModel, Field


class BudgetConfig(BaseModel):
    daily_usd: float | None = None
    monthly_usd: float | None = None


class AnomalyConfig(BaseModel):
    k_mad: float = 4.0  # robust z-score threshold
    min_ratio: float = 2.0  # day must also be >= ratio x baseline median and same-weekday cost
    min_cost_usd: float = 50.0  # ignore entities cheaper than this on the day
    min_history_days: int = 5
    new_entity_usd: float = 200.0  # first-seen entities spending this much in a day alert anyway


class UntaggedConfig(BaseModel):
    required_tags: list[str] = Field(default_factory=list)  # empty disables the rule
    max_share: float = 0.10
    min_cost_usd: float = 50.0


class DevPremiumConfig(BaseModel):
    tag_keys: list[str] = Field(default_factory=lambda: ["env", "environment"])
    tag_values: list[str] = Field(default_factory=lambda: ["dev", "development", "sandbox", "test"])
    flag_photon: bool = True
    flag_serverless: bool = True
    min_cost_usd: float = 10.0


class ComputeConfig(BaseModel):
    max_autotermination_minutes: int = 120
    max_warehouse_auto_stop_minutes: int = 60
    long_running_hours: float = 12.0


class RuleConfig(BaseModel):
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    untagged: UntaggedConfig = Field(default_factory=UntaggedConfig)
    dev_premium: DevPremiumConfig = Field(default_factory=DevPremiumConfig)
    compute: ComputeConfig = Field(default_factory=ComputeConfig)
    digest_top_n: int = 10

"""
General trading engine, strategies, and risk management.
"""

from .data_modules.costs import (
    CostAssumptions,
    CostModel,
    COST_ASSUMPTIONS,
    DEFAULT_COST_MODEL,
    POLYMARKET_ZERO_COST_MODEL,
    SpreadEstimator,
    SlippageModel,
    LiquidityFilter,
    LiquidityFilterConfig,
)
# from .execution import (
#     FillResult,
#     TradeBasedExecutionEngine,
#     LOBExecutionEngine,
# )
from .engine import TradingEngine, EngineConfig, BacktestResult, Event, EventType
from .risk import (
    RiskManager,
    RiskLimits,
    RiskModule,
    RiskCheckResult,
    RiskAction,
    PositionLimitModule,
    ExposureLimitModule,
    DrawdownModule,
    MarketQualityModule,
    VolatilitySizingModule,
    load_risk_modules,
    list_risk_modules,
)
from .risk_profiles import (
    RiskProfile,
    RISK_PROFILES,
    get_risk_profile,
    list_risk_profiles,
)
from .data_modules import (
    CATEGORIES,
    categorize_market,
    categorize_markets,
    DEFAULT_DB_PATH,
    load_manifold_data,
    load_markets,
    build_resolution_map,
    train_test_split,
    PredictionMarketDB,
    build_database,
    DataFetcher,
    ensure_data_exists,
)
from .strategy import (
    Strategy,
    StrategyConfig,
    StrategyManager,
    Signal,
    SignalType,
    generate_signals,
)
from . import strategies as _strategies

BaseStrategy = _strategies.BaseStrategy
WhaleFollowingStrategy = _strategies.WhaleFollowingStrategy
CompositeStrategy = _strategies.CompositeStrategy
SmartMoneyStrategy = _strategies.SmartMoneyStrategy
CrossMarketStrategy = _strategies.CrossMarketStrategy
MomentumStrategy = getattr(_strategies, "MomentumStrategy", None)
MeanReversionStrategy = getattr(_strategies, "MeanReversionStrategy", None)
BreakoutStrategy = getattr(_strategies, "BreakoutStrategy", None)
VolatilityBreakoutStrategy = getattr(_strategies, "VolatilityBreakoutStrategy", None)
TimeDecayStrategy = getattr(_strategies, "TimeDecayStrategy", None)
SentimentDivergenceStrategy = getattr(_strategies, "SentimentDivergenceStrategy", None)
from .portfolio import PortfolioConstraints, PositionSizer, PortfolioState
from .reporting import (
    generate_quantstats_report,
    generate_all_reports,
    compute_daily_returns,
    compute_run_metrics,
    diagnose_trades,
    build_run_summary_from_trades,
    build_run_summary_from_backtest,
    RunMetadata,
    RunMetrics,
    RunDiagnostics,
    RunSummary,
    save_trades_csv,
    save_summary_csv,
    save_diagnostics_csv,
)
from .signals import (
    Signal as ClobSignal,
    SignalConfig,
    SignalContext,
    SignalEngine,
    load_polymarket_markets,
    iter_polymarket_clob_markets,
    generate_signals as generate_clob_signals,
    signals_to_dataframe,
)
from .polymarket_backtest import (
    PolymarketBacktestConfig,
    PolymarketBacktestResult,
    ClobPriceStore,
    run_polymarket_backtest,
    print_polymarket_result,
)
from .momentum_signals import (
    generate_momentum_signals,
    generate_momentum_signals_parquet,
    generate_momentum_signals_sqlite,
    signals_dataframe_to_backtest_format,
)
from .momentum_signals_from_trades import (
    generate_momentum_signals_from_trades,
    trades_to_price_history,
)

__all__ = [
    "CostAssumptions",
    "CostModel",
    "COST_ASSUMPTIONS",
    "DEFAULT_COST_MODEL",
    "POLYMARKET_ZERO_COST_MODEL",
    "TradingEngine",
    "EngineConfig",
    "BacktestResult",
    "Event",
    "EventType",
    "RiskManager",
    "RiskLimits",
    "RiskModule",
    "RiskCheckResult",
    "RiskAction",
    "PositionLimitModule",
    "ExposureLimitModule",
    "DrawdownModule",
    "MarketQualityModule",
    "VolatilitySizingModule",
    "load_risk_modules",
    "list_risk_modules",
    "RiskProfile",
    "RISK_PROFILES",
    "get_risk_profile",
    "list_risk_profiles",
    "CATEGORIES",
    "categorize_market",
    "categorize_markets",
    "DEFAULT_DB_PATH",
    "load_manifold_data",
    "load_markets",
    "build_resolution_map",
    "train_test_split",
    "PredictionMarketDB",
    "build_database",
    "DataFetcher",
    "ensure_data_exists",
    "Strategy",
    "StrategyConfig",
    "StrategyManager",
    "Signal",
    "SignalType",
    "generate_signals",
    "BaseStrategy",
    "WhaleFollowingStrategy",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "CompositeStrategy",
    "PortfolioConstraints",
    "PositionSizer",
    "PortfolioState",
    "generate_quantstats_report",
    "generate_all_reports",
    "compute_daily_returns",
    "compute_run_metrics",
    "diagnose_trades",
    "build_run_summary_from_trades",
    "build_run_summary_from_backtest",
    "RunMetadata",
    "RunMetrics",
    "RunDiagnostics",
    "RunSummary",
    "save_trades_csv",
    "save_summary_csv",
    "save_diagnostics_csv",
    "ClobSignal",
    "SignalConfig",
    "SignalContext",
    "SignalEngine",
    "load_polymarket_markets",
    "iter_polymarket_clob_markets",
    "generate_clob_signals",
    "signals_to_dataframe",
    "PolymarketBacktestConfig",
    "PolymarketBacktestResult",
    "ClobPriceStore",
    "run_polymarket_backtest",
    "print_polymarket_result",
    "generate_momentum_signals",
    "generate_momentum_signals_parquet",
    "generate_momentum_signals_sqlite",
    "signals_dataframe_to_backtest_format",
    "generate_momentum_signals_from_trades",
    "trades_to_price_history",
    "SpreadEstimator",
    "SlippageModel",
    "LiquidityFilter",
    "LiquidityFilterConfig",
    "FillResult",
    "TradeBasedExecutionEngine",
    "LOBExecutionEngine",
]

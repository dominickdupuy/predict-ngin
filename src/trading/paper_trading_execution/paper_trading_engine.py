"""
Paper Trading Engine - Simulates trading without real orders.

Reads live data, generates signals, executes paper trades, tracks P&L.
Ready for migration: no server-specific dependencies.
"""

import pandas as pd
import numpy as np
import yaml
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
import json
import logging
from enum import Enum
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ──────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────────────

class TradeStatus(Enum):
    """Trade lifecycle status."""
    SIGNAL = "signal"  # Signal detected
    ENTERED = "entered"  # Position opened
    EXITED = "exited"  # Position closed
    CANCELLED = "cancelled"  # Position cancelled


@dataclass
class PaperTrade:
    """Record of a paper trade."""
    trade_id: str
    timestamp_entry: int  # Unix ms
    timestamp_exit: Optional[int] = None
    market_id: str = ""
    strategy: str = ""
    side: str = ""  # BUY or SELL
    size: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    roi_pct: float = 0.0
    status: str = TradeStatus.SIGNAL.value
    duration_minutes: float = 0.0
    reason_exit: str = "pending"

    def to_dict(self) -> Dict:
        """Convert to dict for JSON serialization."""
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# ALERT SYSTEM
# ──────────────────────────────────────────────────────────────────────────────

class AlertSystem:
    """Send email alerts via Gmail."""

    def __init__(self, config: Dict):
        self.config = config.get("email_alerts", {})
        self.enabled = self.config.get("enabled", False)
        self.logger = logging.getLogger(__name__)

    def send_alert(self, subject: str, message: str, level: str = "INFO") -> bool:
        """Send email alert."""
        if not self.enabled:
            return False

        try:
            # Create email
            msg = MIMEMultipart()
            msg['From'] = self.config['from_email']
            msg['To'] = ", ".join(self.config['to_emails'])
            msg['Subject'] = f"[{level}] {subject}"
            msg.attach(MIMEText(message, 'plain'))

            # Send via Gmail
            with smtplib.SMTP(self.config['smtp_server'], self.config['smtp_port']) as server:
                server.starttls()
                server.login(self.config['from_email'], self.config['app_password'])
                server.send_message(msg)

            self.logger.info(f"Alert sent: {subject}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to send alert: {e}")
            return False


# ──────────────────────────────────────────────────────────────────────────────
# PAPER TRADING ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class PaperTradingEngine:
    """
    Simulates trading without real orders.

    - Generates signals from strategies
    - Enforces position limits
    - Simulates fills
    - Tracks P&L
    - Sends alerts
    """

    def __init__(self, config_path: str = "config/paper_trading.yaml"):
        """Initialize paper trading engine."""
        self.config_path = Path(config_path)
        self.config = self._load_config()

        # Logging
        self._setup_logging()
        self.logger = logging.getLogger(__name__)

        # State
        self.capital = self.config['trading']['initial_capital']
        self.deployed = {}  # market_id -> size_usd
        self.trades: List[PaperTrade] = []
        self.active_trades: Dict[str, PaperTrade] = {}  # trade_id -> trade

        # Daily/monthly tracking
        self.daily_pnl = 0.0
        self.monthly_pnl = 0.0
        self.trading_active = True

        # Alerts
        self.alerts = AlertSystem(self.config['monitoring'])

        # Create output directories
        self._setup_output_dirs()

        self.logger.info(f"Paper trading engine initialized: ${self.capital} capital")

    def _load_config(self) -> Dict:
        """Load YAML configuration."""
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def _setup_logging(self):
        """Configure logging."""
        log_dir = Path(self.config['monitoring']['log_file']).parent
        log_dir.mkdir(parents=True, exist_ok=True)

        logging.basicConfig(
            level=self.config['monitoring']['log_level'],
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            handlers=[
                logging.FileHandler(self.config['monitoring']['log_file']),
                logging.StreamHandler()
            ]
        )

    def _setup_output_dirs(self):
        """Create output directories."""
        out_dir = Path(self.config['data']['output_dir'])
        out_dir.mkdir(parents=True, exist_ok=True)

    def can_trade(self, market_id: str, size_usd: float, category: str) -> Tuple[bool, str]:
        """Check if position can be opened."""
        # Check 1: Risk limits active?
        if not self.trading_active:
            return False, "Trading halted (loss limits)"

        # Check 2: Enough capital?
        available = self.capital - sum(self.deployed.values())
        if size_usd > available:
            return False, f"Insufficient capital: {size_usd} > {available}"

        # Check 3: Max per market?
        if size_usd > self.config['position_limits']['max_per_market']:
            return False, f"Exceeds per-market limit: {size_usd} > {self.config['position_limits']['max_per_market']}"

        # Check 4: Max per category?
        cat_deployed = sum(
            v for k, v in self.deployed.items()
            if self._market_to_category(k) == category
        )
        if cat_deployed + size_usd > self.config['position_limits']['max_per_category']:
            return False, f"Exceeds per-category limit: {cat_deployed + size_usd} > {self.config['position_limits']['max_per_category']}"

        return True, "OK"

    def _market_to_category(self, market_id: str) -> str:
        """Map market to category (simplified)."""
        # In real system, would lookup market metadata
        # For now, return dummy
        return "general"

    def enter_trade(
        self,
        market_id: str,
        strategy: str,
        side: str,
        size_usd: float,
        entry_price: float,
        current_time: int,
    ) -> Optional[PaperTrade]:
        """Open a paper trade."""
        trade_id = f"{strategy}_{market_id}_{current_time}"

        trade = PaperTrade(
            trade_id=trade_id,
            timestamp_entry=current_time,
            market_id=market_id,
            strategy=strategy,
            side=side,
            size=size_usd,
            entry_price=entry_price,
            status=TradeStatus.ENTERED.value,
        )

        self.active_trades[trade_id] = trade
        self.deployed[market_id] = self.deployed.get(market_id, 0) + size_usd

        self.logger.info(f"Trade entered: {strategy} {side} {size_usd:.0f} USD @ {entry_price:.2f}")
        return trade

    def exit_trade(
        self,
        trade_id: str,
        exit_price: float,
        current_time: int,
        reason: str = "timeout",
    ) -> Optional[PaperTrade]:
        """Close a paper trade."""
        if trade_id not in self.active_trades:
            return None

        trade = self.active_trades.pop(trade_id)
        trade.timestamp_exit = current_time
        trade.exit_price = exit_price
        trade.reason_exit = reason

        # Calculate PnL
        if trade.side == "BUY":
            trade.pnl = trade.size * (exit_price - trade.entry_price)
        else:  # SELL
            trade.pnl = trade.size * (trade.entry_price - exit_price)

        # Apply 2% fee
        trade.pnl *= 0.98

        trade.roi_pct = 100 * trade.pnl / trade.size if trade.size > 0 else 0
        trade.duration_minutes = (current_time - trade.timestamp_entry) / (1000 * 60)
        trade.status = TradeStatus.EXITED.value

        # Update tracking
        self.trades.append(trade)
        self.deployed[trade.market_id] = max(0, self.deployed.get(trade.market_id, 0) - trade.size)
        self.daily_pnl += trade.pnl
        self.monthly_pnl += trade.pnl

        # Check risk limits
        if self.daily_pnl < self.config['risk_limits']['daily_loss_limit']:
            self.trading_active = False
            self.logger.warning("Daily loss limit hit - STOPPING TRADES")
            self.alerts.send_alert(
                "Daily Loss Limit Hit",
                f"Daily PnL: ${self.daily_pnl:.0f}",
                "CRITICAL"
            )

        self.logger.info(f"Trade exited: {trade.strategy} {trade.side} PnL ${trade.pnl:.0f}")
        return trade

    def get_active_trades(self) -> List[PaperTrade]:
        """Get all active positions."""
        return list(self.active_trades.values())

    def check_timeouts(self, current_time: int) -> List[PaperTrade]:
        """Close trades that exceeded max hold time."""
        closed = []
        max_hold_ms = self.config['risk_limits']['max_hold_minutes'] * 60 * 1000

        for trade_id, trade in list(self.active_trades.items()):
            age_ms = current_time - trade.timestamp_entry
            if age_ms > max_hold_ms:
                # Simulate exit at current mid-price (assume no change)
                exit_price = (trade.entry_price + 0.01) if trade.side == "BUY" else (trade.entry_price - 0.01)
                exited = self.exit_trade(trade_id, exit_price, current_time, reason="timeout")
                if exited:
                    closed.append(exited)

        return closed

    def get_stats(self) -> Dict:
        """Get P&L statistics."""
        if len(self.trades) == 0:
            return {'error': 'No trades yet'}

        trades_df = pd.DataFrame([t.to_dict() for t in self.trades])

        return {
            'total_trades': len(self.trades),
            'winning_trades': (trades_df['pnl'] > 0).sum(),
            'losing_trades': (trades_df['pnl'] < 0).sum(),
            'win_rate_pct': 100 * (trades_df['pnl'] > 0).sum() / len(self.trades),
            'total_pnl': trades_df['pnl'].sum(),
            'avg_pnl': trades_df['pnl'].mean(),
            'median_pnl': trades_df['pnl'].median(),
            'max_win': trades_df['pnl'].max(),
            'max_loss': trades_df['pnl'].min(),
            'daily_pnl': self.daily_pnl,
            'monthly_pnl': self.monthly_pnl,
            'capital_remaining': self.capital + self.monthly_pnl,
            'roi_pct': 100 * self.monthly_pnl / self.capital if self.capital > 0 else 0,
        }

    def save_trades(self):
        """Save trades to Parquet."""
        if len(self.trades) == 0:
            return

        trades_df = pd.DataFrame([t.to_dict() for t in self.trades])
        output_file = Path(self.config['data']['trade_log'])
        output_file.parent.mkdir(parents=True, exist_ok=True)

        trades_df.to_parquet(output_file, index=False)
        self.logger.info(f"Trades saved: {output_file}")

    def save_equity(self):
        """Save equity curve."""
        equity_value = self.capital + self.monthly_pnl

        # Append to CSV
        output_file = Path(self.config['data']['pnl_log'])
        output_file.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'equity': equity_value,
            'pnl': self.monthly_pnl,
            'active_trades': len(self.active_trades),
        }

        # Append to file
        with open(output_file, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def report_daily(self) -> str:
        """Generate daily P&L report."""
        stats = self.get_stats()

        report = f"""
╔════════════════════════════════════════════╗
║  PAPER TRADING DAILY REPORT                ║
║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}              ║
╚════════════════════════════════════════════╝

Capital:              ${self.capital:>10.0f}
Monthly P&L:         ${self.monthly_pnl:>10.0f}
ROI:                 {stats.get('roi_pct', 0):>10.1f}%
Equity:              ${stats.get('capital_remaining', 0):>10.0f}

Trades:
  Total:             {stats.get('total_trades', 0):>10}
  Winning:           {stats.get('winning_trades', 0):>10}
  Losing:            {stats.get('losing_trades', 0):>10}
  Win Rate:          {stats.get('win_rate_pct', 0):>9.1f}%

P&L:
  Total:             ${stats.get('total_pnl', 0):>10.0f}
  Average:           ${stats.get('avg_pnl', 0):>10.0f}
  Max Win:           ${stats.get('max_win', 0):>10.0f}
  Max Loss:          ${stats.get('max_loss', 0):>10.0f}

Active Positions:    {len(self.active_trades):>10}
Status:              {'ACTIVE' if self.trading_active else 'HALTED':>10}
"""
        return report

    def send_daily_report(self):
        """Send daily report via email."""
        if not self.config['monitoring']['daily_report']['enabled']:
            return

        report = self.report_daily()
        stats = self.get_stats()

        email_body = f"""
Paper Trading Daily Report

{report}

Strategy Breakdown:
{self._get_strategy_breakdown()}

Active Positions:
{self._get_active_positions_summary()}

Next Steps:
- Check logs at: {self.config['monitoring']['log_file']}
- Trade log: {self.config['data']['trade_log']}
- Monitor live at: scripts/live/run_paper_trading.py
"""

        self.alerts.send_alert(
            "Daily P&L Report",
            email_body,
            "INFO"
        )

    def _get_strategy_breakdown(self) -> str:
        """Get PnL by strategy."""
        if len(self.trades) == 0:
            return "No trades"

        trades_df = pd.DataFrame([t.to_dict() for t in self.trades])
        breakdown = ""

        for strategy in trades_df['strategy'].unique():
            strat_trades = trades_df[trades_df['strategy'] == strategy]
            pnl = strat_trades['pnl'].sum()
            count = len(strat_trades)
            wr = 100 * (strat_trades['pnl'] > 0).sum() / count if count > 0 else 0
            breakdown += f"  {strategy:20s}: {count:3d} trades, ${pnl:8.0f} PnL, {wr:5.1f}% WR\n"

        return breakdown

    def _get_active_positions_summary(self) -> str:
        """Summarize active positions."""
        if len(self.active_trades) == 0:
            return "No active positions"

        summary = ""
        for trade_id, trade in self.active_trades.items():
            age_min = (datetime.now(timezone.utc).timestamp() * 1000 - trade.timestamp_entry) / (1000 * 60)
            summary += f"  {trade.strategy:15s} {trade.side:4s} ${trade.size:7.0f} @ {trade.entry_price:.2f} (age: {age_min:.0f}min)\n"

        return summary


# ──────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTION
# ──────────────────────────────────────────────────────────────────────────────

def get_engine(config_path: str = "config/paper_trading.yaml") -> PaperTradingEngine:
    """Get or create paper trading engine."""
    return PaperTradingEngine(config_path)


if __name__ == "__main__":
    # Test
    engine = get_engine()
    print(engine.report_daily())

#!/usr/bin/env python3
"""
Paper Trading Real-Time Dashboard

Plotly Dash app that displays:
- Real-time trades and P&L
- Active positions
- Equity curve
- Summary metrics

Run: python scripts/dashboard/paper_trading_dashboard.py
Then navigate to http://localhost:8050
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Dash, dcc, html, Input, Output
from pathlib import Path
from datetime import datetime
import json

# Data paths
TRADES_FILE = Path("data/paper_trading/trades.parquet")
EQUITY_FILE = Path("data/paper_trading/equity.parquet")

# Initialize Dash app
app = Dash(__name__)
app.title = "Paper Trading Dashboard"

# Styling
colors = {
    'background': '#0f1117',
    'text': '#c9d1d9',
    'win': '#3fb950',
    'loss': '#f85149',
    'neutral': '#58a6ff'
}

def load_trades():
    """Load trades from parquet file."""
    if not TRADES_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(TRADES_FILE)
    except Exception as e:
        print(f"Error loading trades: {e}")
        return pd.DataFrame()

def load_equity():
    """Load equity curve from parquet file."""
    if not EQUITY_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(EQUITY_FILE)
    except Exception as e:
        print(f"Error loading equity: {e}")
        return pd.DataFrame()

def get_summary_metrics(trades_df):
    """Calculate summary metrics from trades."""
    if trades_df.empty:
        return {
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'roi_pct': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'max_win': 0,
            'max_loss': 0,
            'sharpe': 0,
        }

    trades_df = trades_df.copy()
    if 'pnl' not in trades_df.columns:
        trades_df['pnl'] = 0

    # Filled trades only
    filled = trades_df[trades_df.get('status', '') == 'exited']

    total_trades = len(filled)
    if total_trades == 0:
        return {
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'roi_pct': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'max_win': 0,
            'max_loss': 0,
            'sharpe': 0,
        }

    total_pnl = filled['pnl'].sum()
    wins = filled[filled['pnl'] > 0]
    losses = filled[filled['pnl'] < 0]

    win_rate = len(wins) / total_trades if total_trades > 0 else 0
    avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
    max_win = filled['pnl'].max()
    max_loss = filled['pnl'].min()

    # Sharpe ratio (simple: daily returns std)
    if 'timestamp_exit' in filled.columns and len(filled) > 1:
        sharpe = (filled['pnl'].sum() / filled['pnl'].std()) if filled['pnl'].std() > 0 else 0
    else:
        sharpe = 0

    return {
        'total_trades': total_trades,
        'win_rate': win_rate * 100,
        'total_pnl': total_pnl,
        'roi_pct': (total_pnl / 1000) * 100,  # Assuming $1000 starting capital
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'max_win': max_win,
        'max_loss': max_loss,
        'sharpe': sharpe,
    }

def create_metric_card(label, value, unit=''):
    """Create a metric card."""
    if isinstance(value, float):
        if unit == '%':
            formatted = f"{value:.1f}%"
        elif unit == '$':
            formatted = f"${value:.2f}"
        else:
            formatted = f"{value:.2f}"
    else:
        formatted = str(value)

    return html.Div(
        [
            html.Div(label, style={'font-size': '12px', 'color': colors['text'], 'opacity': 0.7}),
            html.Div(formatted, style={'font-size': '24px', 'font-weight': 'bold', 'color': colors['neutral']})
        ],
        style={
            'padding': '15px',
            'background': '#161b22',
            'border-radius': '8px',
            'border-left': f'4px solid {colors["neutral"]}',
        }
    )

@app.callback(
    [
        Output('summary-metrics', 'children'),
        Output('equity-chart', 'figure'),
        Output('trades-table', 'children'),
        Output('pnl-distribution', 'figure'),
        Output('active-positions', 'children'),
    ],
    Input('update-interval', 'n_intervals'),
)
def update_dashboard(_):
    """Update all dashboard components."""
    trades_df = load_trades()
    equity_df = load_equity()
    metrics = get_summary_metrics(trades_df)

    # ─── Summary Metrics Row ───
    summary = html.Div(
        [
            create_metric_card('Total Trades', metrics['total_trades']),
            create_metric_card('Win Rate', metrics['win_rate'], '%'),
            create_metric_card('Total P&L', metrics['total_pnl'], '$'),
            create_metric_card('ROI', metrics['roi_pct'], '%'),
            create_metric_card('Sharpe', metrics['sharpe']),
            create_metric_card('Max Win', metrics['max_win'], '$'),
        ],
        style={
            'display': 'grid',
            'grid-template-columns': 'repeat(auto-fit, minmax(150px, 1fr))',
            'gap': '10px',
            'margin-bottom': '20px',
        }
    )

    # ─── Equity Curve ───
    if equity_df.empty:
        equity_fig = go.Figure()
        equity_fig.add_annotation(text="No equity data yet", xref="paper", yref="paper", showarrow=False)
    else:
        if 'timestamp' in equity_df.columns and 'equity' in equity_df.columns:
            equity_fig = go.Figure(
                data=[go.Scatter(x=equity_df['timestamp'], y=equity_df['equity'], mode='lines+markers', name='Equity')],
                layout=go.Layout(
                    title='Equity Curve',
                    hovermode='x unified',
                    plot_bgcolor=colors['background'],
                    paper_bgcolor=colors['background'],
                    font=dict(color=colors['text']),
                    margin=dict(l=40, r=20, t=40, b=40),
                )
            )
            equity_fig.update_xaxes(showgrid=False)
            equity_fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='#30363d')
        else:
            equity_fig = go.Figure()
            equity_fig.add_annotation(text="No equity chart data", xref="paper", yref="paper", showarrow=False)

    # ─── Recent Trades Table ───
    if trades_df.empty:
        trades_table = html.Div("No trades yet", style={'color': colors['text'], 'padding': '20px'})
    else:
        # Only show exited trades
        display_trades = trades_df[trades_df.get('status', '') == 'exited'].tail(10).copy()
        if display_trades.empty:
            trades_table = html.Div("No completed trades yet", style={'color': colors['text'], 'padding': '20px'})
        else:
            # Format for display
            cols_to_show = ['strategy', 'side', 'size', 'entry_price', 'exit_price', 'pnl']
            available_cols = [c for c in cols_to_show if c in display_trades.columns]

            rows = []
            for _, trade in display_trades.iterrows():
                pnl = trade.get('pnl', 0)
                pnl_color = colors['win'] if pnl > 0 else colors['loss'] if pnl < 0 else colors['neutral']
                rows.append(
                    html.Tr([
                        html.Td(trade.get('strategy', ''), style={'color': colors['neutral']}),
                        html.Td(trade.get('side', ''), style={'color': colors['neutral']}),
                        html.Td(f"${trade.get('size', 0):.0f}", style={'color': colors['neutral']}),
                        html.Td(f"{trade.get('entry_price', 0):.4f}", style={'color': colors['neutral']}),
                        html.Td(f"{trade.get('exit_price', 0):.4f}", style={'color': colors['neutral']}),
                        html.Td(f"${pnl:.2f}", style={'color': pnl_color, 'font-weight': 'bold'}),
                    ])
                )

            trades_table = html.Table(
                [
                    html.Thead(html.Tr([html.Th(c.upper(), style={'color': colors['text'], 'text-align': 'left', 'padding': '10px'}) for c in ['Strategy', 'Side', 'Size', 'Entry', 'Exit', 'P&L']])),
                    html.Tbody(rows)
                ],
                style={'width': '100%', 'border-collapse': 'collapse', 'font-size': '12px'}
            )

    # ─── P&L Distribution ───
    if trades_df.empty or 'pnl' not in trades_df.columns:
        pnl_fig = go.Figure()
        pnl_fig.add_annotation(text="No P&L data", xref="paper", yref="paper", showarrow=False)
    else:
        filled = trades_df[trades_df.get('status', '') == 'exited']
        if filled.empty:
            pnl_fig = go.Figure()
            pnl_fig.add_annotation(text="No filled trades yet", xref="paper", yref="paper", showarrow=False)
        else:
            pnl_fig = go.Figure(
                data=[go.Histogram(x=filled['pnl'], nbinsx=20, name='P&L')],
                layout=go.Layout(
                    title='P&L Distribution',
                    xaxis_title='P&L ($)',
                    yaxis_title='Count',
                    hovermode='x',
                    plot_bgcolor=colors['background'],
                    paper_bgcolor=colors['background'],
                    font=dict(color=colors['text']),
                    margin=dict(l=40, r=20, t=40, b=40),
                )
            )
            pnl_fig.update_traces(marker_color=colors['neutral'])
            pnl_fig.update_xaxes(showgrid=False)
            pnl_fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='#30363d')

    # ─── Active Positions ───
    active = trades_df[trades_df.get('status', '') == 'entered']
    if active.empty:
        active_positions = html.Div("No active positions", style={'color': colors['text'], 'padding': '20px', 'font-style': 'italic'})
    else:
        rows = []
        for _, trade in active.iterrows():
            rows.append(
                html.Tr([
                    html.Td(trade.get('strategy', ''), style={'color': colors['neutral']}),
                    html.Td(trade.get('side', ''), style={'color': colors['neutral']}),
                    html.Td(f"${trade.get('size', 0):.0f}", style={'color': colors['neutral']}),
                    html.Td(f"{trade.get('entry_price', 0):.4f}", style={'color': colors['neutral']}),
                ])
            )

        active_positions = html.Table(
            [
                html.Thead(html.Tr([html.Th(c.upper(), style={'color': colors['text'], 'text-align': 'left', 'padding': '10px'}) for c in ['Strategy', 'Side', 'Size', 'Entry']])),
                html.Tbody(rows)
            ],
            style={'width': '100%', 'border-collapse': 'collapse', 'font-size': '12px'}
        )

    return summary, equity_fig, trades_table, pnl_fig, active_positions

# ─── Layout ───
app.layout = html.Div(
    [
        dcc.Interval(id='update-interval', interval=5000, n_intervals=0),  # Update every 5 seconds

        html.Div(
            [
                html.H1('Paper Trading Dashboard', style={'color': colors['text'], 'margin': 0}),
                html.Div(id='update-time', style={'color': colors['text'], 'opacity': 0.7, 'font-size': '12px'}),
            ],
            style={'padding': '20px', 'border-bottom': f'1px solid #30363d', 'margin-bottom': '20px'}
        ),

        html.Div(
            [
                # Summary Metrics
                html.Div(id='summary-metrics'),

                # Charts Row
                html.Div(
                    [
                        html.Div(
                            dcc.Graph(id='equity-chart', style={'height': '400px'}),
                            style={'flex': 1, 'min-width': '300px'}
                        ),
                        html.Div(
                            dcc.Graph(id='pnl-distribution', style={'height': '400px'}),
                            style={'flex': 1, 'min-width': '300px'}
                        ),
                    ],
                    style={'display': 'flex', 'gap': '20px', 'margin-bottom': '20px', 'flex-wrap': 'wrap'}
                ),

                # Active Positions
                html.Div(
                    [
                        html.H3('Active Positions', style={'color': colors['text'], 'margin-top': 0}),
                        html.Div(id='active-positions'),
                    ],
                    style={'background': '#161b22', 'padding': '15px', 'border-radius': '8px', 'margin-bottom': '20px'}
                ),

                # Recent Trades
                html.Div(
                    [
                        html.H3('Recent Trades', style={'color': colors['text'], 'margin-top': 0}),
                        html.Div(id='trades-table'),
                    ],
                    style={'background': '#161b22', 'padding': '15px', 'border-radius': '8px'}
                ),
            ],
            style={'padding': '20px'}
        ),
    ],
    style={
        'background': colors['background'],
        'color': colors['text'],
        'font-family': '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        'min-height': '100vh',
        'padding': 0,
        'margin': 0,
    }
)

if __name__ == '__main__':
    import sys
    import io
    # Fix unicode encoding on Windows
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    print("[DASHBOARD] Starting Paper Trading Dashboard...")
    print("            Navigate to: http://localhost:8050")
    print("            Press Ctrl+C to stop")
    app.run(debug=False, host='0.0.0.0', port=8050)

import os
import json
import math
import datetime as dt
from dataclasses import dataclass
import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# ccxt는 백테스트만 쓸 땐 없어도 되지만, 시세 수집하려면 설치 필요
try:
    import ccxt
except Exception:
    ccxt = None

APP_TITLE = "Siu Autotrade GUI — Supertrend 77 (v2)"

# ---------------------- Helpers ----------------------
def ema(series: pd.Series, period: int):
    return series.ewm(span=period, adjust=False).mean()

def atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    h, l, c = high.values, low.values, close.values
    tr = np.zeros_like(c, dtype=float)
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    return pd.Series(tr, index=close.index).ewm(alpha=1/period, adjust=False).mean()

def supertrend77(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """
    Supertrend 77 (ATR 기반). 항상보유/플립 전략의 베이스 라인 계산.
    입력 df: columns = ['timestamp','open','high','low','close','volume'] (timestamp는 pandas.Timestamp)
    """
    df = df.copy()
    hl2 = (df['high'] + df['low']) / 2.0
    _atr = atr_series(df['high'], df['low'], df['close'], period=period)
    upper = hl2 + multiplier * _atr
    lower = hl2 - multiplier * _atr

    st_line = [np.nan] * len(df)
    st_dir  = [False] * len(df)

    # 초기화
    st_line[0] = upper.iloc[0]
    st_dir[0]  = df['close'].iloc[0] >= st_line[0]

    for i in range(1, len(df)):
        # trailing 형태 유지 (클래식 구현의 핵심)
        upper_i = upper.iloc[i] if (upper.iloc[i] < upper.iloc[i-1] or df['close'].iloc[i-1] > upper.iloc[i-1]) else upper.iloc[i-1]
        lower_i = lower.iloc[i] if (lower.iloc[i] > lower.iloc[i-1] or df['close'].iloc[i-1] < lower.iloc[i-1]) else lower.iloc[i-1]

        prev_line = st_line[i-1]
        if prev_line == upper.iloc[i-1]:
            st_line[i] = upper_i if df['close'].iloc[i] <= upper_i else lower_i
        else:
            st_line[i] = lower_i if df['close'].iloc[i] >= lower_i else upper_i

        st_dir[i] = df['close'].iloc[i] >= st_line[i]

    df['st_line'] = st_line
    df['st_dir']  = st_dir
    df['flip']    = df['st_dir'].ne(pd.Series(st_dir).shift(1)).fillna(False)
    df['atr']     = _atr
    return df

@dataclass
class Trade:
    side: str           # 'long' or 'short'
    entry_idx: int
    entry_time: pd.Timestamp
    entry_price: float
    exit_idx: int = None
    exit_time: pd.Timestamp = None
    exit_price: float = None
    pnl_abs: float = 0.0
    pnl_pct: float = 0.0

# 백테스트 (TP/SL: ATR 배수, 동일 봉 히트 처리)
def backtest_supertrend(
    df: pd.DataFrame,
    notional_usdt: float = 100.0,
    fee_rate: float = 0.0006,          # 테이커 한쪽 수수료(예: 0.06% = 0.0006)
    st_period: int = 10,
    st_mult: float = 3.0,
    tp_atr_mult: float = 0.0,          # 0이면 비활성
    sl_atr_mult: float = 0.0,          # 0이면 비활성
    conservative_stop_first: bool = True,  # 동일 봉 TP/SL 동시 충족 시 SL 우선
):
    data = supertrend77(df, period=st_period, multiplier=st_mult)
    trades = []
    pos = None

    for i in range(1, len(data)):
        prev = data.iloc[i-1]
        cur  = data.iloc[i]
        open_price = cur['open']

        flipped_up   = (prev['st_dir'] is False) and (cur['st_dir'] is True)
        flipped_down = (prev['st_dir'] is True)  and (cur['st_dir'] is False)

        # 1) 플립 시 기존 포지션 청산 (현재봉 시가로)
        do_open = None
        if flipped_up or flipped_down:
            if pos is not None:
                exit_price = open_price
                gross_ret = (exit_price - pos.entry_price)/pos.entry_price if pos.side == 'long' else (pos.entry_price - exit_price)/pos.entry_price
                fees = notional_usdt * fee_rate * 2
                pnl_abs = notional_usdt * gross_ret - fees
                pnl_pct = pnl_abs / notional_usdt * 100.0

                pos.exit_idx = i
                pos.exit_time = cur['timestamp']
                pos.exit_price = exit_price
                pos.pnl_abs = pnl_abs
                pos.pnl_pct = pnl_pct
                trades.append(pos)
                pos = None

            do_open = 'long' if flipped_up else 'short'

        # 2) TP/SL 체크 (오픈 포지션 보유 중일 때, 현재봉 고가/저가로 인바 체크)
        if pos is not None and (tp_atr_mult > 0.0 or sl_atr_mult > 0.0):
            entry_atr = data['atr'].iloc[pos.entry_idx]
            if pos.side == 'long':
                tp = pos.entry_price * (1 + (entry_atr * tp_atr_mult) / pos.entry_price) if tp_atr_mult > 0 else None
                sl = pos.entry_price * (1 - (entry_atr * sl_atr_mult) / pos.entry_price) if sl_atr_mult > 0 else None

                hit_tp = (tp is not None) and (cur['high'] >= tp)
                hit_sl = (sl is not None) and (cur['low']  <= sl)
                decided = False

                if hit_tp and hit_sl:
                    exit_price = sl if conservative_stop_first else tp
                    decided = True
                elif hit_tp:
                    exit_price = tp; decided = True
                elif hit_sl:
                    exit_price = sl; decided = True

                if decided:
                    gross_ret = (exit_price - pos.entry_price)/pos.entry_price
                    fees = notional_usdt * fee_rate * 2
                    pnl_abs = notional_usdt * gross_ret - fees
                    pnl_pct = pnl_abs / notional_usdt * 100.0
                    pos.exit_idx = i
                    pos.exit_time = cur['timestamp']
                    pos.exit_price = exit_price
                    pos.pnl_abs = pnl_abs
                    pos.pnl_pct = pnl_pct
                    trades.append(pos)
                    pos = None

            else:  # short
                tp = pos.entry_price * (1 - (entry_atr * tp_atr_mult) / pos.entry_price) if tp_atr_mult > 0 else None
                sl = pos.entry_price * (1 + (entry_atr * sl_atr_mult) / pos.entry_price) if sl_atr_mult > 0 else None

                hit_tp = (tp is not None) and (cur['low']  <= tp)
                hit_sl = (sl is not None) and (cur['high'] >= sl)
                decided = False

                if hit_tp and hit_sl:
                    exit_price = sl if conservative_stop_first else tp
                    decided = True
                elif hit_tp:
                    exit_price = tp; decided = True
                elif hit_sl:
                    exit_price = sl; decided = True

                if decided:
                    gross_ret = (pos.entry_price - exit_price)/pos.entry_price
                    fees = notional_usdt * fee_rate * 2
                    pnl_abs = notional_usdt * gross_ret - fees
                    pnl_pct = pnl_abs / notional_usdt * 100.0
                    pos.exit_idx = i
                    pos.exit_time = cur['timestamp']
                    pos.exit_price = exit_price
                    pos.pnl_abs = pnl_abs
                    pos.pnl_pct = pnl_pct
                    trades.append(pos)
                    pos = None

        # 3) 새로운 포지션 오픈(플립 직후)
        if do_open and pos is None:
            side = do_open
            pos = Trade(
                side=side,
                entry_idx=i,
                entry_time=cur['timestamp'],
                entry_price=open_price
            )

    # 마지막 봉에서 미청산 포지션 정리
    if pos is not None:
        last = data.iloc[-1]
        exit_price = last['close']
        gross_ret = (exit_price - pos.entry_price)/pos.entry_price if pos.side == 'long' else (pos.entry_price - exit_price)/pos.entry_price
        fees = notional_usdt * fee_rate * 2
        pnl_abs = notional_usdt * gross_ret - fees
        pnl_pct = pnl_abs / notional_usdt * 100.0
        pos.exit_idx = len(data)-1
        pos.exit_time = last['timestamp']
        pos.exit_price = exit_price
        pos.pnl_abs = pnl_abs
        pos.pnl_pct = pnl_pct
        trades.append(pos)

    # 거래 DF 구성
    rows = []
    for t in trades:
        rows.append({
            "side": t.side,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "pnl_abs_usdt": round(t.pnl_abs, 6),
            "pnl_pct": round(t.pnl_pct, 6),
        })
    trades_df = pd.DataFrame(rows)

    # 에쿼티 커브 & 지표
    starting_balance = 1000.0
    eq = [starting_balance]
    for v in trades_df["pnl_abs_usdt"].tolist():
        eq.append(eq[-1] + v)
    equity = pd.Series(eq, name="equity")

    roll_max = equity.cummax()
    dd = (equity - roll_max)
    max_dd = float(dd.min()) if len(dd) else 0.0

    rets = trades_df["pnl_pct"] / 100.0 if len(trades_df) else pd.Series([], dtype=float)
    sharpe = float((rets.mean() / (rets.std() + 1e-12))) if len(rets) > 1 else 0.0

    if len(trades_df):
        trades_df["month"] = trades_df["exit_time"].dt.to_period("M").astype(str)
        monthly = trades_df.groupby("month")["pnl_abs_usdt"].sum().reset_index()
    else:
        monthly = pd.DataFrame(columns=["month","pnl_abs_usdt"])

    summary = {
        "total_pnl": float(trades_df["pnl_abs_usdt"].sum()) if len(trades_df) else 0.0,
        "win_rate": float((trades_df["pnl_abs_usdt"] > 0).mean()*100) if len(trades_df) else 0.0,
        "num_trades": int(len(trades_df)),
        "max_drawdown": max_dd,
        "sharpe_per_trade": sharpe,
        "equity": equity.tolist(),
    }
    return trades_df, monthly, summary

def ensure_columns(df):
    needed = ["timestamp","open","high","low","close","volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"Input DataFrame missing columns: {missing}")
    return df

def fetch_ohlcv_ccxt(exchange_name, symbol, timeframe, limit, use_testnet, api_key, api_secret, api_password):
    if ccxt is None:
        raise RuntimeError("ccxt 미설치: pip install ccxt")

    if exchange_name == "bitget":
        ex = ccxt.bitget({
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "password": api_password or "",
            "options": {"defaultType": "swap"},
        })
    elif exchange_name == "bybit":
        ex = ccxt.bybit({
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "options": {"defaultType": "swap"},
        })
    elif exchange_name == "binance":
        ex = ccxt.binance({
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "options": {"defaultType": "future"},
        })
    else:
        raise ValueError("지원되지 않는 거래소")

    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df

# ---------------------- UI ----------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)
st.caption("API 키 입력 → 조건값 설정 → 실행 → 거래 리스트·수익률·에쿼티 커브·MDD/샤프. (기본: 백테스트)")

with st.sidebar:
    st.header("① 연결/시장")
    ex_name = st.selectbox("거래소", ["bitget","bybit","binance"])
    use_testnet = st.checkbox("테스트넷", value=True)

    st.subheader("API 키")
    api_key = st.text_input("API KEY", type="password")
    api_secret = st.text_input("API SECRET", type="password")
    api_password = st.text_input("API PASSWORD (Bitget/Bybit)", type="password")

    st.subheader("심볼/주기")
    symbol = st.text_input("심볼 (ccxt 표기)", value="BTC/USDT:USDT")
    timeframe = st.selectbox("캔들 주기", ["1m","3m","5m","15m","30m","1h","4h","1d"], index=2)
    limit = st.slider("가져올 캔들 수", min_value=200, max_value=2000, value=800, step=100)

    st.header("② 전략 (Supertrend 77)")
    st_period = st.number_input("ATR Period", min_value=5, max_value=100, value=10, step=1)
    st_mult = st.number_input("ATR Multiplier", min_value=1.0, max_value=10.0, value=3.0, step=0.1)

    st.header("③ TP/SL (ATR 배수)")
    tp_atr_mult = st.number_input("TP ATR 배수 (0=off)", min_value=0.0, max_value=10.0, value=0.0, step=0.1)
    sl_atr_mult = st.number_input("SL ATR 배수 (0=off)", min_value=0.0, max_value=10.0, value=0.0, step=0.1)
    conservative_stop_first = st.checkbox("동일 봉에서 TP/SL 동시 충족 시 SL 우선(보수적)", value=True)

    st.header("④ 리스크/수수료")
    notional_usdt = st.number_input("트레이드 당 Notional (USDT)", min_value=10.0, max_value=100000.0, value=200.0, step=10.0)
    fee_rate_pct = st.number_input("테이커 수수료(한쪽, %)", min_value=0.0, max_value=0.5, value=0.06, step=0.01)
    fee_rate = fee_rate_pct / 100.0

    st.header("⑤ 모드")
    mode = st.radio("실행 모드", ["백테스트","라이브(실험)"], index=0)

    st.header("설정 저장/불러오기")
    save_keys = st.checkbox("API 키를 .env에 저장", value=False)
    settings = {
        "exchange": ex_name, "testnet": use_testnet,
        "symbol": symbol, "timeframe": timeframe, "limit": int(limit),
        "st_period": int(st_period), "st_mult": float(st_mult),
        "tp_atr_mult": float(tp_atr_mult), "sl_atr_mult": float(sl_atr_mult),
        "conservative_stop_first": bool(conservative_stop_first),
        "notional_usdt": float(notional_usdt), "fee_rate_pct": float(fee_rate_pct),
        "mode": mode,
    }
    st.download_button("현재 설정 JSON 다운로드", data=json.dumps(settings, ensure_ascii=False, indent=2),
                       file_name="siu77_settings.json")

    uploaded = st.file_uploader("설정 JSON 불러오기", type=["json"])
    if uploaded is not None:
        try:
            incoming = json.load(uploaded)
            st.session_state['loaded_settings'] = incoming
            st.success("설정 불러오기 완료. 위 입력란을 수동으로 반영하세요.")
        except Exception as e:
            st.error(f"설정 파일 파싱 실패: {e}")

# 실행 버튼
colA, colB = st.columns([1,1])
with colA:
    run_btn = st.button("▶ 실행", use_container_width=True, type="primary")
with colB:
    st.caption("실거래 전 충분한 테스트를 권장합니다.")

st.markdown("---")

if run_btn:
    # .env 저장 (선택)
    if save_keys:
        with open(".env","w", encoding="utf-8") as f:
            f.write(f"EXCHANGE={ex_name}\nUSE_TESTNET={'true' if use_testnet else 'false'}\n")
            f.write(f"API_KEY={api_key}\nAPI_SECRET={api_secret}\nAPI_PASSWORD={api_password}\n")
            f.write(f"SYMBOL={symbol}\nTIMEFRAME={timeframe}\n")
        st.success(".env 저장 완료")

    # 시세 수집
    try:
        if ccxt is None:
            raise RuntimeError("ccxt 미설치: pip install ccxt")
        df = fetch_ohlcv_ccxt(ex_name, symbol, timeframe, limit, use_testnet, api_key, api_secret, api_password)
    except Exception as e:
        st.error(f"시세 수집 실패: {e}")
        st.stop()

    try:
        ensure_columns(df)
    except Exception as e:
        st.error(str(e))
        st.stop()

    with st.expander("원본 캔들 데이터 (상위 10행)"):
        st.dataframe(df.head(10), use_container_width=True)

    if mode == "백테스트":
        trades_df, monthly_df, summary = backtest_supertrend(
            df,
            notional_usdt=notional_usdt,
            fee_rate=fee_rate,
            st_period=st_period,
            st_mult=st_mult,
            tp_atr_mult=tp_atr_mult,
            sl_atr_mult=sl_atr_mult,
            conservative_stop_first=conservative_stop_first,
        )

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("총 손익(USDT)", f"{summary['total_pnl']:,.2f}")
        c2.metric("거래 수", f"{summary['num_trades']}")
        c3.metric("승률(%)", f"{summary['win_rate']:.2f}")
        c4.metric("최대낙폭(MDD, USDT)", f"{summary['max_drawdown']:,.2f}")
        c5.metric("샤프(거래당)", f"{summary['sharpe_per_trade']:.2f}")

        st.subheader("에쿼티 커브")
        fig = plt.figure()
        plt.plot(summary["equity"])
        plt.xlabel("Trade #")
        plt.ylabel("Equity (USDT)")
        st.pyplot(fig)

        st.subheader("거래 리스트")
        if len(trades_df):
            st.dataframe(trades_df, use_container_width=True, hide_index=True)
            csv = trades_df.to_csv(index=False).encode("utf-8")
            st.download_button("거래내역 CSV 다운로드", data=csv, file_name="trades.csv")
        else:
            st.info("거래가 없습니다. 파라미터/기간을 조정하세요.")

        st.subheader("월별 손익")
        if len(monthly_df):
            st.dataframe(monthly_df, use_container_width=True, hide_index=True)
        else:
            st.info("월별 손익 데이터가 없습니다.")

    else:
        st.warning("라이브(실험) 모드: 주문 코드는 기본 비활성화. 테스트넷에서 충분히 검증 후 사용하세요.")
        st.code("""
# 주문 예시 (거래소/심볼/정밀도 확인!)
# order = ex.create_order(symbol, type="market", side="buy", amount=qty)
# 실전에는 웹소켓으로 포지션/체결 동기화, 레이트리밋, 재시도, 로깅 등이 필요합니다.
""", language="python")

st.markdown("---")
st.caption("⚠️ 교육/연구용 예시입니다. 실제 거래는 본인 책임이며, 거래소 약관과 현지 규제를 준수하세요.")

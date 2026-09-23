"""
나스닥100(QQQ) 사상 최고가 대비 낙폭을 계산해 QLD/TQQQ의 매수·익절·손절 tier에
새로 도달했는지 판단하고, 결과를 state.json에 기록한다.

- 이 스크립트는 GitHub Actions에서 정기 실행된다. Claude 클라우드 루틴 실행 환경은
  직접 네트워크 호출(yfinance 등)이 막혀있어 정밀 계산이 어렵기 때문에, 정확한 계산은
  일반 네트워크 접근이 가능한 GitHub Actions 쪽에서 담당하고, 계산 결과만 state.json에
  남긴다. 실제 카카오톡 알림 전송은 별도의 Claude 클라우드 루틴이 이 state.json을 읽어서
  수행한다 (이 스크립트는 카카오톡을 직접 보내지 않는다).

tier별 상태는 WAIT(대기) -> PENDING(tier 도달, 반등확인 대기) -> IN(매수 완료) 순으로
진행하고, IN 상태에서는 두 가지 매도 조건을 감시한다:
  - 익절: 고점대비 낙폭이 진입 tier보다 RECOVERY_STEP_PCT(%p)만큼 개선되면 매도.
    예: -20%에서 산 물량은 -10%까지 회복하면 매도, -10%에서 산 물량은 신고가에서 매도.
  - 손절(QLD만): 그 물량 자체의 진입가 대비 손익이 QLD_STOP_LOSS_PCT 이하로 빠지면
    매도. 손절되면 WAIT가 아니라 COOLDOWN으로 가서, 낙폭이 그 tier 위로 완전히
    회복해야만(=재하락이 아니라는 게 확인돼야만) 다시 PENDING을 받을 수 있다.
    (쿨다운 없이 바로 재무장하면 하락이 계속될 때 손절→즉시재진입→재손절이 반복돼
    무의미해지기 때문. 백테스트로 검증된 설계.)
  - TQQQ는 애초에 깊은 tier(-45%~)에서만, 반등확인까지 거쳐 진입하므로 손절을 따로
    두지 않는다.

각 tier는 배정된 원화 예산을 그 시점 QLD/TQQQ 가격으로 나눠 "몇 주"를 살지 계산해서
shares에 기록하고, 매도 시에는 그 tier가 실제로 산 주식수 그대로 전량 매도한다.
"""
import json
from pathlib import Path

import yfinance as yf

STATE_PATH = Path(__file__).parent / "state.json"

TICKER = "QQQ"  # 나스닥100 추종 ETF, 사상 최고가/현재가 기준 (tier·반등확인 판단용)

# 낙폭 트리거 tier (고점 대비 하락률, %). QQQ 낙폭 기준.
QLD_TIERS = [-10, -15, -20, -25, -30, -35]
TQQQ_TIERS = [-45, -50, -55, -60, -65, -70, -75]  # 닷컴버블급 전용 리저브

# 상품별 총 예산(원화). 각 tier는 이걸 tier 개수로 균등분할해서 받는다.
# 순자산의 10%(2026-09-24 기준 순자산 2.2억 -> 위성 예산 2,200만) 기준으로 산정.
# 순자산이 유의미하게 바뀌면 이 비율 기준으로 다시 계산할 것.
QLD_BUDGET_KRW = 13_200_000  # 위성 예산의 60%
TQQQ_BUDGET_KRW = 8_800_000  # 위성 예산의 40%
TIER_BUDGET_KRW = {t: QLD_BUDGET_KRW / len(QLD_TIERS) for t in QLD_TIERS}
TIER_BUDGET_KRW.update({t: TQQQ_BUDGET_KRW / len(TQQQ_TIERS) for t in TQQQ_TIERS})

RECOVERY_STEP_PCT = 10  # 익절: 진입 tier보다 낙폭이 이만큼(%p) 개선되면 매도
QLD_STOP_LOSS_PCT = -20  # QLD 전용. 진입가 대비 이 비율 이하로 빠지면 손절 (TQQQ는 None)

# 반등 확인 기준
LOCAL_LOW_LOOKBACK_DAYS = 15
BOUNCE_PCT_THRESHOLD = 3.0
CONSECUTIVE_UP_DAYS = 2

FALLBACK_USD_KRW_RATE = 1500.0


def default_tiers() -> dict:
    tiers = {}
    for t in QLD_TIERS:
        tiers[f"QLD:{t}"] = {"status": "WAIT", "shares": 0, "entry_price": None}
    for t in TQQQ_TIERS:
        tiers[f"TQQQ:{t}"] = {"status": "WAIT", "shares": 0, "entry_price": None}
    return tiers


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("tiers", default_tiers())
        return state
    return {"tiers": default_tiers(), "pending_alert": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def get_usd_krw_rate() -> float:
    try:
        hist = yf.Ticker("KRW=X").history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return FALLBACK_USD_KRW_RATE


def get_price(ticker: str) -> float:
    hist = yf.Ticker(ticker).history(period="5d")
    if hist.empty:
        raise RuntimeError(f"{ticker} 가격 데이터를 가져오지 못했습니다.")
    return float(hist["Close"].iloc[-1])


def check_bounce(closes) -> dict:
    """최근 종가 시계열(closes, 오래된 순)로 반등 확인 여부를 판단한다."""
    recent = closes[-LOCAL_LOW_LOOKBACK_DAYS:]
    local_low = float(min(recent))
    current = float(closes[-1])
    bounce_pct = (current - local_low) / local_low * 100

    consecutive_up = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            consecutive_up += 1
        else:
            break

    confirmed = bounce_pct >= BOUNCE_PCT_THRESHOLD or consecutive_up >= CONSECUTIVE_UP_DAYS
    return {
        "local_low": round(local_low, 2),
        "bounce_pct": round(bounce_pct, 2),
        "consecutive_up_days": consecutive_up,
        "confirmed": confirmed,
    }


def process_tiers(tiers, product, tier_list, price, stop_loss_pct, drawdown_pct, bounce, buy_msgs, sell_msgs):
    """tier 목록 하나(QLD 또는 TQQQ)의 상태를 전이시키고 필요한 알림 문구를 채운다."""
    for t in tier_list:
        key = f"{product}:{t}"
        pos = tiers.setdefault(key, {"status": "WAIT", "shares": 0, "entry_price": None})

        # 쿨다운 해제: 낙폭이 그 tier 위로 완전히 회복
        if pos["status"] == "COOLDOWN" and drawdown_pct > t:
            pos["status"] = "WAIT"

        # 신규 tier 도달 -> 대기(반등확인 전)
        if pos["status"] == "WAIT" and drawdown_pct <= t:
            pos["status"] = "PENDING"

        # 반등확인 -> 매수, 주식수는 그 시점 가격으로 계산해서 고정
        if pos["status"] == "PENDING" and bounce["confirmed"]:
            budget_usd = TIER_BUDGET_KRW[t] / get_usd_krw_rate_cached()
            shares = int(budget_usd // price)
            if shares > 0:
                pos["status"] = "IN"
                pos["shares"] = shares
                pos["entry_price"] = round(price, 2)
                buy_msgs.append(f"{product}{t}%tier {shares}주매수")
            # 예산으로 1주도 못 사면 PENDING 유지, 다음 실행 때 재시도

        # 보유 중이면 익절/손절 조건 감시
        if pos["status"] == "IN":
            take_profit = drawdown_pct >= t + RECOVERY_STEP_PCT or drawdown_pct >= -0.5
            stop_loss = False
            if stop_loss_pct is not None and pos["entry_price"]:
                rel_pct = (price / pos["entry_price"] - 1) * 100
                stop_loss = rel_pct <= stop_loss_pct

            if take_profit:
                sell_msgs.append(f"{product}{t}%tier {pos['shares']}주익절매도")
                pos["status"] = "WAIT"
                pos["shares"] = 0
                pos["entry_price"] = None
            elif stop_loss:
                sell_msgs.append(f"{product}{t}%tier {pos['shares']}주손절매도")
                pos["status"] = "COOLDOWN"
                pos["shares"] = 0
                pos["entry_price"] = None


_usd_krw_cache = None


def get_usd_krw_rate_cached() -> float:
    global _usd_krw_cache
    if _usd_krw_cache is None:
        _usd_krw_cache = get_usd_krw_rate()
    return _usd_krw_cache


def main() -> None:
    hist = yf.Ticker(TICKER).history(period="max")
    if hist.empty:
        raise RuntimeError(f"{TICKER} 가격 데이터를 가져오지 못했습니다.")

    closes = hist["Close"].tolist()
    current_price = float(closes[-1])
    ath = float(hist["Close"].max())
    ath_date = hist["Close"].idxmax().strftime("%Y-%m-%d")
    drawdown_pct = (current_price - ath) / ath * 100  # 항상 0 이하

    bounce = check_bounce(closes)

    qld_price = get_price("QLD")
    tqqq_price = get_price("TQQQ")
    usd_krw_rate = get_usd_krw_rate_cached()

    state = load_state()
    tiers = state["tiers"]

    buy_msgs = []
    sell_msgs = []

    process_tiers(tiers, "QLD", QLD_TIERS, qld_price, QLD_STOP_LOSS_PCT, drawdown_pct, bounce, buy_msgs, sell_msgs)
    process_tiers(tiers, "TQQQ", TQQQ_TIERS, tqqq_price, None, drawdown_pct, bounce, buy_msgs, sell_msgs)

    messages = []
    if sell_msgs:
        messages.append(f"🔻QQQ{drawdown_pct:.1f}% " + ", ".join(sell_msgs))
    if buy_msgs:
        messages.append(
            f"🔔QQQ고점({ath_date})대비{drawdown_pct:.1f}%, 반등+{bounce['bounce_pct']:.1f}% "
            + ", ".join(buy_msgs)
        )

    if messages:
        state["pending_alert"] = "\n".join(messages)
    state.setdefault("pending_alert", None)

    state["tiers"] = tiers
    state["ticker"] = TICKER
    state["current_price"] = round(current_price, 2)
    state["ath"] = round(ath, 2)
    state["ath_date"] = ath_date
    state["drawdown_pct"] = round(drawdown_pct, 2)
    state["bounce"] = bounce
    state["qld_price"] = round(qld_price, 2)
    state["tqqq_price"] = round(tqqq_price, 2)
    state["usd_krw_rate"] = round(usd_krw_rate, 2)

    save_state(state)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

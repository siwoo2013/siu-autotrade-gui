# 상단 import에 추가
from urllib.parse import urlencode

def _request(
    self,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = self.BASE_URL + path
    ts = self._timestamp_ms()

    # ✅ 쿼리 문자열 “순서 고정”
    query = ""
    ordered_params = None
    if method.upper() == "GET" and params:
        # 키 정렬(사전식)로 고정
        ordered_params = [(k, params[k]) for k in sorted(params.keys())]
        query = "?" + urlencode(ordered_params)  # e.g. marginCoin=USDT&symbol=BTCUSDT_UMCBL
        url = url + query                        # ✅ 실제 요청 URL에도 같은 문자열 사용

    raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
    # ✅ 서명도 위에서 만든 query 그대로 사용
    sign = self._sign(ts, method, path + query, raw_body)

    headers = {
        "ACCESS-KEY": self.api_key,
        "ACCESS-PASSPHRASE": self.passphrase,
        "ACCESS-SIGN-TYPE": self.SIGN_TYPE,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-SIGN": sign,
        "Content-Type": "application/json",
    }

    resp = self.session.request(
        method=method.upper(),
        url=url,                             # ✅ params를 다시 주지 않음(순서 깨질 수 있음)
        headers=headers,
        params=None,                         # ← 중요: GET이라도 None
        data=raw_body if method.upper() != "GET" else None,
        timeout=self.timeout,
    )

    if not (200 <= resp.status_code < 300):
        try:
            detail = resp.json()
        except Exception:
            detail = {"raw": resp.text}
        self.log.error(
            "Bitget HTTP %s %s -> %s | url=%s | body=%s",
            method.upper(), path, resp.status_code, resp.url, raw_body if raw_body else ""
        )
        self.log.error("Bitget response: %s", detail)
        resp.raise_for_status()

    return resp.json()

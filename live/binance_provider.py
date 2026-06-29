"""Binance Live Data Provider — REST + WebSocket."""
from __future__ import annotations
import json
import threading
import time
from typing import Optional, Callable, List, Dict, Any
import urllib.request
import urllib.parse


BINANCE_REST_BASE = "https://api.binance.com"
BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"


def fetch_klines_rest(symbol: str, interval: str = "5m", limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch historical klines via REST."""
    url = f"{BINANCE_REST_BASE}/api/v3/klines"
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": min(int(limit), 1000),
    }
    full_url = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full_url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"Binance REST error: {e}")
    
    candles = []
    for k in data:
        candles.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
            "quote_volume": float(k[7]),
            "trades": int(k[8]),
            "taker_buy_base_volume": float(k[9]),
            "taker_buy_quote_volume": float(k[10]),
        })
    return candles


class BinanceWSClient:
    """WebSocket client for live klines."""
    
    def __init__(
        self,
        symbol: str,
        interval: str = "5m",
        on_tick: Optional[Callable[[Dict], None]] = None,
        on_closed: Optional[Callable[[Dict], None]] = None,
    ):
        self.symbol = symbol.lower()
        self.interval = interval
        self.on_tick = on_tick
        self.on_closed = on_closed
        self.stream = f"{self.symbol}@kline_{interval}"
        self.ws_url = f"{BINANCE_WS_BASE}/{self.stream}"
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_event_time: Optional[float] = None
    
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._running = False
    
    def is_alive(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()
    
    def seconds_since_last_event(self) -> Optional[float]:
        if self._last_event_time is None:
            return None
        return time.time() - self._last_event_time
    
    def _run_loop(self):
        backoff = 1
        while self._running:
            try:
                self._connect_and_listen()
                backoff = 1
            except Exception as e:
                print(f"⚠ BinanceWS error: {e}")
                time.sleep(min(backoff, 30))
                backoff *= 2
    
    def _connect_and_listen(self):
        try:
            from websocket import WebSocketApp
        except ImportError:
            print("⚠ websocket-client not installed — falling back to polling mode")
            self._poll_loop()
            return
        
        def on_message(ws, message):
            try:
                msg = json.loads(message)
                k = msg.get("k", {})
                if not k:
                    return
                candle = {
                    "open_time": int(k["t"]),
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k["v"]),
                    "close_time": int(k["T"]),
                    "quote_volume": float(k["q"]),
                    "trades": int(k["n"]),
                    "taker_buy_base_volume": float(k["V"]),
                    "taker_buy_quote_volume": float(k["Q"]),
                }
                self._last_event_time = time.time()
                
                is_closed = bool(k.get("x", False))
                if is_closed:
                    if self.on_closed:
                        try:
                            self.on_closed(candle)
                        except Exception as e:
                            print(f"⚠ on_closed callback error: {e}")
                else:
                    if self.on_tick:
                        try:
                            self.on_tick(candle)
                        except Exception as e:
                            print(f"⚠ on_tick callback error: {e}")
            except Exception as e:
                print(f"⚠ WS message parse error: {e}")
        
        def on_error(ws, error):
            print(f"⚠ WS error: {error}")
        
        def on_close(ws, code, msg):
            pass
        
        ws = WebSocketApp(
            self.ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=20, ping_timeout=10)
    
    def _poll_loop(self):
        """Fallback: REST polling every 2 seconds."""
        last_open_time = None
        while self._running:
            try:
                candles = fetch_klines_rest(self.symbol.upper(), self.interval, limit=2)
                if candles:
                    current = candles[-1]
                    self._last_event_time = time.time()
                    if last_open_time is not None and current["open_time"] > last_open_time:
                        if self.on_closed:
                            try:
                                self.on_closed(candles[-2])
                            except Exception as e:
                                print(f"⚠ poll on_closed error: {e}")
                    if self.on_tick:
                        try:
                            self.on_tick(current)
                        except Exception as e:
                            print(f"⚠ poll on_tick error: {e}")
                    last_open_time = current["open_time"]
            except Exception as e:
                print(f"⚠ poll error: {e}")
            time.sleep(2)

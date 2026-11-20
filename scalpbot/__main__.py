from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path

from .app import INTERVAL_SECONDS, open_bar_start_ms, run_app
from .timeutil import day_key, day_start_ms, describe_boundary, format_ts, tz_label
from .config import (
    SYMBOL_ALIASES,
    SYMBOL_RE,
    Config,
    ConfigError,
    confirmation_from_env,
    load_config,
    validate_config,
)
from .broker import TestnetBroker
from .discovery import select_symbols
from .exchange import BinanceError, BinanceFuturesClient
from .indicators import Candle
from .netinfo import IpDetector
from .orderbook import analyze_depth
from .security import clean_message, ensure_private_dir, ensure_private_file, resolve_under, safe_print as print
from .strategy import ScalpPullbackStrategy

log = logging.getLogger("scalpbot.cli")

VERSION = "1.0.0"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scalpbot",
        description="Bot Binance Futures: Scalping PullBack Tool (PAC/EMA) dengan SL, TP, BE, trailing.",
    )
    parser.add_argument("--version", action="version", version=f"scalpbot {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="jalankan bot")
    run_p.add_argument("-c", "--config", default="config.json", help="path config JSON")
    run_p.add_argument("--live", action="store_true", help="aktifkan mode live (uang nyata)")
    run_p.add_argument("--paper", action="store_true", help="paksa mode paper (tanpa order nyata)")
    run_p.add_argument("--paper-balance", type=float, default=50.0, help="saldo awal mode paper")
    run_p.add_argument("--no-dashboard", action="store_true", help="log biasa tanpa layar alternatif")
    run_p.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")

    check_p = sub.add_parser("check", help="validasi config + koneksi + akun")
    check_p.add_argument("-c", "--config", default="config.json")
    check_p.add_argument("--live", action="store_true", help="izinkan validasi config mode live")

    pnl_p = sub.add_parser(
        "pnl",
        help="lihat PnL hari ini menurut Binance (hari exchange UTC, ditampilkan juga dalam WIB)",
    )
    pnl_p.add_argument("-c", "--config", default="config.json")
    pnl_p.add_argument("--top", type=int, default=8, help="jumlah simbol teratas pada rincian")
    pnl_p.add_argument("--live", action="store_true", help="izinkan mode live")

    book_p = sub.add_parser("bookscan", help="ukur orderbook universe aktif: spread, imbalance, dan peluang lolos filter")
    book_p.add_argument("-c", "--config", default="config.json")
    book_p.add_argument("--live", action="store_true", help="izinkan mode live")
    book_p.add_argument("--samples", type=int, default=6, help="jumlah sampel per simbol")
    book_p.add_argument("--delay", type=float, default=0.5, help="jeda antar sampel (detik)")

    sig_p = sub.add_parser("signals", help="replay sinyal historis untuk dibandingkan dengan TradingView")
    sig_p.add_argument("-c", "--config", default="config.json")
    sig_p.add_argument("--live", action="store_true", help="izinkan mode live")
    sig_p.add_argument("--symbol", default=None, help="override satu simbol")
    sig_p.add_argument("--bars", type=int, default=1000, help="jumlah bar yang diambil (maks 1500)")
    sig_p.add_argument("--csv", default=None, help="simpan hasil ke file CSV")
    sig_p.add_argument(
        "--show-blocked",
        action="store_true",
        help="tampilkan juga sinyal yang ditolak filter rezim",
    )
    return parser


def _load(args: argparse.Namespace) -> tuple[Config, list[str]]:
    cfg = load_config(args.config)
    if getattr(args, "paper", False):
        cfg.mode = "paper"
    if getattr(args, "log_level", None):
        cfg.logging.level = str(args.log_level).upper()
    if getattr(args, "no_dashboard", False):
        cfg.dashboard.enabled = False
    warnings = validate_config(cfg, live_requested=bool(getattr(args, "live", False)))
    _authorize_mode(cfg, args)
    return cfg, warnings


def _live_gate(cfg, args) -> bool:
    if cfg.mode != "live":
        return True
    if confirmation_from_env():
        return True
    if sys.stdin.isatty():
        print("MODE LIVE: order nyata akan dikirim ke Binance.")
        try:
            answer = input("Ketik LIVE untuk melanjutkan: ").strip()
        except EOFError:
            print()
            answer = ""
        if answer == "LIVE":
            return True
    print("Dibatalkan. Set SCALPBOT_LIVE_CONFIRM=YES-LIVE atau ketik LIVE saat diminta.")
    return False


def _authorize_mode(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "paper", False) and getattr(args, "live", False):
        raise ConfigError("--live dan --paper tidak boleh dipakai bersamaan")
    if cfg.mode != "live":
        cfg.live_authorized = False
        return
    if not getattr(args, "live", False):
        raise ConfigError("mode live butuh flag --live pada semua subcommand")
    if not _live_gate(cfg, args):
        raise ConfigError("konfirmasi mode live ditolak")
    cfg.live_authorized = True


def cmd_run(args: argparse.Namespace) -> int:
    cfg, warnings = _load(args)
    if not math.isfinite(args.paper_balance) or args.paper_balance <= 0:
        print("--paper-balance harus angka hingga yang lebih besar dari nol")
        return 2
    for w in warnings:
        print(f"[peringatan] {w}")
    if cfg.mode in {"testnet", "live"} and not (cfg.api_key() and cfg.api_secret()):
        print(
            f"API key belum ada. Set env {cfg.api.key_env} dan {cfg.api.secret_env} "
            f"(testnet: https://testnet.binancefuture.com)."
        )
        return 2
    return asyncio.run(run_app(cfg, paper_balance=args.paper_balance))


def _risk_ceiling(cfg: Config, sl_bps: float) -> float:
    """Risiko maksimum (% equity) yang bisa dicapai, pada SL selebar sl_bps.

    Dua pembatas: `max_notional_frac_equity` (dibatasi 1.0 di `size_position`) dan
    guard margin di `try_open` yang menuntut margin <= 98% dari saldo tersedia.
    """
    frac = min(cfg.risk.max_notional_frac_equity, 1.0, 0.98)
    return cfg.risk.leverage * frac * sl_bps / 100.0


def cmd_check(args: argparse.Namespace) -> int:
    cfg, warnings = _load(args)
    print(f"config      : {Path(args.config).resolve()}")
    print(f"mode        : {cfg.mode}")
    print(f"interval    : {cfg.market.interval} ({INTERVAL_SECONDS[cfg.market.interval]}s)")
    print(f"discovery   : {cfg.market.discovery}")
    if cfg.market.discovery == "top_volume":
        print(
            f"  top_n     : {cfg.market.discovery_top_n}\n"
            f"  min volume: {cfg.market.discovery_quote_volume_min:,.0f} USDT (24h)\n"
            f"  refresh   : {cfg.market.discovery_refresh_s}s\n"
            f"  exclude   : {', '.join(cfg.market.discovery_exclude) or '-'}\n"
            f"  wajib muat: {cfg.market.discovery_require_affordable}"
        )
    if cfg.market.discovery == "static":
        print(f"simbol      : {', '.join(cfg.market.symbols)}")
    else:
        print(
            f"simbol      : {', '.join(cfg.market.symbols) or '-'} "
            f"(fallback, daftar aktif dipilih oleh discovery)"
        )
    print(f"risk        : {cfg.risk.risk_pct}% / trade, leverage {cfg.risk.leverage}x, SL {cfg.risk.sl_mode}")
    batas = _risk_ceiling(cfg, cfg.risk.sl_max_bps)
    print(
        f"  batas     : notional maks {cfg.risk.leverage}x x {min(cfg.risk.max_notional_frac_equity, 1.0, 0.98):g} = "
        f"{cfg.risk.leverage * min(cfg.risk.max_notional_frac_equity, 1.0, 0.98):g}x equity; pada SL terlebar "
        f"{cfg.risk.sl_max_bps:g} bps itu setara risiko {batas:.2f}%"
        + (
            f". risk_pct {cfg.risk.risk_pct:g}% akan DIPOTONG ke sekitar {batas:.2f}%"
            if cfg.risk.risk_pct > batas
            else ""
        )
    )
    for w in warnings:
        print(f"[peringatan] {w}")
    client = BinanceFuturesClient(cfg)
    print(f"log teknis  : {cfg.logging.file_for(cfg.mode)}")
    print(f"log event   : {cfg.logging.events_for(cfg.mode)}")
    if cfg.logging.persist_state:
        state_path = Path(cfg.logging.state_for(cfg.mode))
        info = "belum ada (akan dibuat saat entry pertama)"
        if state_path.is_file():
            try:
                blob = json.loads(
                    state_path.read_text(encoding="utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
                cooldown = len(blob.get("cooldown") or {})
                entries = len(blob.get("entries") or [])
                info = (
                    f"{cooldown} simbol dengan cooldown, {entries} baris riwayat, "
                    f"disimpan {blob.get('saved_at', '?')}"
                )
            except (OSError, ValueError) as exc:
                info = f"tidak terbaca ({exc})"
        print(f"state       : {state_path} ({info})")
    else:
        print("state       : dimatikan (logging.persist_state=false)")
    print(f"REST base   : {client.base_url}")
    print(f"WS base     : {cfg.ws_url()}")
    try:
        try:
            offset = client.sync_time()
            print(f"server time : offset {offset} ms (recvWindow {cfg.api.recv_window_ms} ms)")
            filters = client.load_all_filters()
            if cfg.market.discovery == "top_volume":
                equity = 0.0
                if cfg.mode != "paper" and cfg.api_key() and cfg.api_secret():
                    try:
                        equity = client.usdt_equity()[0]
                    except BinanceError as exc:
                        print(f"saldo       : tidak terbaca ({exc}), cek muat dimatikan")
                selection = select_symbols(
                    client.ticker_24hr(),
                    tradable=client.tradable_symbols(),
                    filters=filters,
                    exclude=cfg.market.discovery_exclude,
                    min_quote_volume=cfg.market.discovery_quote_volume_min,
                    top_n=cfg.market.discovery_top_n,
                    equity=equity,
                    leverage=cfg.risk.leverage,
                    max_notional_frac=cfg.risk.max_notional_frac_equity,
                    require_affordable=cfg.market.discovery_require_affordable and equity > 0,
                )
                print(f"kandidat    : {selection.summary(limit=10) or '-'}")
                if not selection.symbols:
                    print("peringatan  : discovery kosong, memakai fallback market.symbols")
                active_symbols = selection.symbols or list(cfg.market.symbols)
            else:
                active_symbols = list(cfg.market.symbols)
            tradable = set(client.tradable_symbols())
            for sym in active_symbols:
                flt = filters.get(sym)
                if flt is None or sym not in tradable:
                    print(f"  {sym:<10} TIDAK TRADABLE (butuh PERPETUAL, TRADING, quote USDT)")
                    continue
                print(
                    f"  {sym:<10} tick={flt.tick_size:g} step={flt.step_size:g} "
                    f"minQty={flt.min_qty:g} minNotional={flt.min_notional:g}"
                )
            if cfg.mode != "paper":
                if not (cfg.api_key() and cfg.api_secret()):
                    print("akun        : dilewati (API key belum diset)")
                else:
                    balance, available = client.usdt_equity()
                    print(f"akun        : saldo {balance:.4f} USDT, available {available:.4f} USDT")
                    pos = [p for p in client.position_risk() if float(p.get("positionAmt", 0)) != 0]
                    print(f"posisi      : {len(pos)} terbuka")
                    for row in pos:
                        print(f"  {row['symbol']} {row['positionAmt']} @ {row.get('entryPrice')}")
        except BinanceError as exc:
            print(f"koneksi     : GAGAL {exc}")
            return 1
        if cfg.net.ip_check:
            det = IpDetector(
                cfg.net.ip_url,
                cfg.net.ip_check_interval_s,
                cfg.net.proxy,
                fallback=cfg.net.ip_fallback,
            )
            ip = det.fetch_once()
            print(f"IP publik   : {ip or 'tidak terdeteksi'} (whitelist IP ini di API key bila diaktifkan)")
    finally:
        client.close()
    print("status      : OK")
    return 0


def cmd_bookscan(args: argparse.Namespace) -> int:
    """Ukur seberapa sering filter orderbook menolak entry, pakai data depth nyata."""
    cfg, warnings = _load(args)
    for w in warnings:
        print(f"[peringatan] {w}")
    client = BinanceFuturesClient(cfg)
    try:
        filters = client.load_all_filters()
        if cfg.market.discovery == "top_volume":
            selection = select_symbols(
                client.ticker_24hr(),
                tradable=client.tradable_symbols(),
                filters=filters,
                exclude=cfg.market.discovery_exclude,
                min_quote_volume=cfg.market.discovery_quote_volume_min,
                top_n=cfg.market.discovery_top_n,
                equity=0.0,
                leverage=cfg.risk.leverage,
                max_notional_frac=cfg.risk.max_notional_frac_equity,
                require_affordable=False,
            )
            symbols = selection.symbols or list(cfg.market.symbols)
        else:
            symbols = list(cfg.market.symbols)
        if not symbols:
            print("tidak ada simbol untuk dipindai")
            return 1
        ob = cfg.orderbook
        print(f"orderbook filter : {'aktif' if ob.enabled else 'mati'}")
        print(f"ambang           : |imbalance| >= {ob.min_imbalance:.2f}, spread <= {ob.max_spread_bps:.1f}bps")
        print(f"simbol           : {len(symbols)} ({cfg.market.discovery}), sampel {args.samples}x per simbol\n")
        spreads: dict[str, list[float]] = {s: [] for s in symbols}
        imbs: dict[str, list[float]] = {s: [] for s in symbols}
        for i in range(max(1, args.samples)):
            for sym in symbols:
                try:
                    payload = client.depth(sym, ob.depth_limit)
                except BinanceError as exc:
                    log.info("depth %s gagal: %s", sym, exc)
                    continue
                snap = analyze_depth(sym, payload, ob.depth_limit)
                if snap is None:
                    continue
                spreads[sym].append(snap.spread_bps)
                imbs[sym].append(snap.imbalance)
            if i + 1 < args.samples:
                time.sleep(max(0.0, args.delay))
    finally:
        client.close()

    if not ob.enabled:
        print("filter orderbook dimatikan: tidak ada entry yang ditolak.")
        return 0
    print(f"{'SYMBOL':<12}{'spread p50':>11}{'spread max':>11}{'|imb| p50':>11}{'|imb| max':>11}{'lolos':>9}")
    total_pass = 0
    total_n = 0
    dead: list[str] = []
    spread_block: list[str] = []
    imb_block: list[str] = []
    tight: list[str] = []
    for sym in symbols:
        sp = spreads[sym]
        im = [abs(x) for x in imbs[sym]]
        if not sp:
            print(f"{sym:<12}{'n/a':>11}")
            continue
        passed = 0
        blocked_sp = 0
        blocked_imb = 0
        for s_v, i_v in zip(sp, im, strict=False):
            bad_sp = s_v > ob.max_spread_bps
            bad_imb = i_v < ob.min_imbalance
            if bad_sp:
                blocked_sp += 1
            if bad_imb:
                blocked_imb += 1
            if not bad_sp and not bad_imb:
                passed += 1
        total_pass += passed
        total_n += len(sp)
        p50_sp = statistics.median(sp)
        p50_im = statistics.median(im)
        rate = passed / len(sp)
        if rate < 0.4:
            tight.append(sym)
            if rate == 0.0:
                dead.append(sym)
            if blocked_sp > blocked_imb:
                spread_block.append(sym)
            elif blocked_imb > blocked_sp:
                imb_block.append(sym)
        print(
            f"{sym:<12}{p50_sp:>11.2f}{max(sp):>11.2f}{p50_im:>11.3f}{max(im):>11.3f}"
            f"{passed:>5}/{len(sp):<3}"
        )
    if total_n:
        print(f"\ntotal lolos: {total_pass}/{total_n} = {total_pass / total_n * 100:.0f}% percobaan entry")
    if dead:
        print(f"selalu ditolak ({len(dead)}): {', '.join(dead)}")
    if spread_block:
        print(f"  penyebab spread terlalu lebar: {', '.join(spread_block)}")
    if imb_block:
        print(f"  penyebab imbalance terlalu kecil: {', '.join(imb_block)}")
    if tight and not dead:
        print(f"sering ditolak ({len(tight)}): {', '.join(tight)}")
    print("\nSaran: ambang imbalance adalah gerbang tunggal paling sering memblokir.")
    print("  min_imbalance 0.15 -> 0.00 : filter imbalance jadi tidak memblokir (spread tetap dijaga)")
    print("  min_imbalance 0.35        : hanya blokir saat book sangat berat sebelah")
    return 0


def cmd_pnl(args: argparse.Namespace) -> int:
    """Tampilkan asal angka DAY di dashboard, dalam dua jendela waktu yang jelas.

    Penting dibedakan karena keduanya berbeda 7 jam untuk WIB:

    * **hari bot** (00:00 WIB), inilah yang dipakai `daily_loss_limit_pct`.
    * **hari exchange** (00:00 UTC), inilah yang tampil di aplikasi Binance.
    """
    cfg, warnings = _load(args)
    for w in warnings:
        print(f"[peringatan] {w}")
    if cfg.mode == "paper":
        print("mode paper: tidak ada exchange, DAY hanya menghitung sesi berjalan (selalu mulai dari 0).")
        print("Gunakan config testnet/live untuk melihat PnL harian dari Binance.")
        return 0
    tz = cfg.logging.timezone
    label = tz_label(tz)
    bot_start_ms = day_start_ms(tz)
    client = BinanceFuturesClient(cfg)
    broker = TestnetBroker(client, cfg)
    try:
        bot_info = broker.income_since(bot_start_ms, label=day_key(tz))
        utc_info = broker.income_today()
    finally:
        client.close()
    if bot_info is None or utc_info is None:
        print("gagal membaca /fapi/v1/income. Cek izin baca futures pada API key dan koneksi.")
        return 1
    limit_pct = cfg.risk.daily_loss_limit_pct
    kinds = bot_info["kinds"]
    print(f"Jendela yang dipakai bot: 00:00 {label} (hari bot), dasar batas rugi harian")
    print(f"  REALIZED_PNL : {kinds.get('REALIZED_PNL', 0.0):+10.4f} USDT")
    print(f"  COMMISSION   : {kinds.get('COMMISSION', 0.0):+10.4f} USDT  (fee)")
    print(f"  FUNDING_FEE  : {kinds.get('FUNDING_FEE', 0.0):+10.4f} USDT")
    print(f"  {'-' * 34}")
    print(f"  TOTAL        : {bot_info['total']:+10.4f} USDT   <- angka DAY di dashboard")
    print()
    by_symbol = bot_info["by_symbol"]
    if by_symbol:
        ranked = sorted(by_symbol.items(), key=lambda kv: kv[1])[: max(1, args.top)]
        print(f"Per simbol (paling rugi {len(ranked)} dari {len(by_symbol)}):")
        for symbol, value in ranked:
            print(f"  {symbol:<14} {value:+10.4f}")
        print()
    print(
        f"Jendela Binance (hari exchange UTC {utc_info['day']}, "
        f"dimulai {describe_boundary(tz)} {label}): TOTAL {utc_info['total']:+10.4f} USDT"
    )
    print("  Ini yang tampil di aplikasi Binance; sengaja tidak dipakai sebagai batas harian.")
    print()
    if limit_pct > 0:
        try:
            equity, _ = broker.equity()
        except BinanceError as exc:
            print(f"batas harian : {limit_pct:.2f}% (equity tidak terbaca: {exc})")
            return 0
        cap = -equity * limit_pct / 100.0
        print(f"equity       : {equity:.2f} USDT")
        print(f"batas harian : {limit_pct:.2f}% = {cap:+.2f} USDT (halt bila TOTAL hari bot <= batas)")
        print(f"status       : {'HALT' if bot_info['total'] <= cap else 'masih boleh entry'}")
    else:
        print("batas harian : dimatikan (daily_loss_limit_pct = 0)")
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    cfg, warnings = _load(args)
    for w in warnings:
        print(f"[peringatan] {w}")
    if args.symbol:
        sym = clean_message(str(args.symbol).strip()).upper()
        sym = SYMBOL_ALIASES.get(sym, sym)
        if not SYMBOL_RE.fullmatch(sym):
            print("simbol tidak valid")
            return 2
        symbols = [sym]
    else:
        symbols = list(cfg.market.symbols)
    client = BinanceFuturesClient(cfg)
    try:
        client.sync_time()
        if not args.symbol and cfg.market.discovery == "top_volume":
            filters = client.load_all_filters()
            selection = select_symbols(
                client.ticker_24hr(),
                tradable=client.tradable_symbols(),
                filters=filters,
                exclude=cfg.market.discovery_exclude,
                min_quote_volume=cfg.market.discovery_quote_volume_min,
                top_n=cfg.market.discovery_top_n,
                equity=0.0,
                leverage=cfg.risk.leverage,
                max_notional_frac=cfg.risk.max_notional_frac_equity,
                require_affordable=False,
            )
            print(f"discovery   : {selection.summary() or '-'}")
            if selection.symbols:
                symbols = selection.symbols
            else:
                symbols = list(cfg.market.symbols)
                print("peringatan  : discovery kosong, memakai fallback market.symbols")
        if not symbols:
            print("tidak ada simbol untuk direplay")
            return 1
        bars = max(300, min(args.bars, 1500))
        probe = ScalpPullbackStrategy("-", cfg.strategy, cfg.risk.atr_len).required_bars()
        if bars < probe:
            bars = min(probe + 20, 1500)
            print(
                f"catatan     : bars dinaikkan ke {bars} agar filter rezim punya data cukup "
                f"(butuh {probe} bar)"
            )
        open_ts = open_bar_start_ms(client.server_time_ms(), INTERVAL_SECONDS[cfg.market.interval])
        rows: list[list[str]] = []
        for sym in symbols:
            candles: list[Candle] = client.klines(sym, cfg.market.interval, limit=bars)
            closed = [c for c in candles if c.ts < open_ts]
            strat = ScalpPullbackStrategy(sym, cfg.strategy, cfg.risk.atr_len)
            signals = 0
            armed = 0
            blocked = 0
            for c in closed:
                state = strat.on_bar(c)
                if state.signal:
                    armed += 1
                    allowed = strat.regime_allows(state)
                    if not allowed:
                        blocked += 1
                        if not args.show_blocked:
                            continue
                    signals += 1
                    stamp = format_ts(state.ts / 1000, cfg.logging.timezone, "%Y-%m-%d %H:%M")
                    mark = "" if allowed else "  [ditolak filter rezim]"
                    print(
                        f"{stamp} {sym:<10} {state.signal:<5} close={state.price:<12.6g} "
                        f"PAC={state.pac_center:<12.6g} ATR={state.atr:<10.6g} "
                        f"trend={state.trend:+d}{mark}"
                    )
                    rows.append(
                        [
                            stamp,
                            sym,
                            state.signal,
                            f"{state.price:.8g}",
                            f"{state.pac_center:.8g}",
                            f"{state.atr:.8g}",
                            str(state.trend),
                        ]
                    )
            tail = f", {blocked} ditolak filter rezim" if armed else ""
            print(
                f"--> {sym}: {signals} sinyal lolos dari {armed} sinyal mentah, "
                f"{len(closed)} bar {cfg.market.interval} (jam {tz_label(cfg.logging.timezone)}){tail}"
            )
        if args.csv and rows:
            try:
                path = resolve_under(args.csv)
                ensure_private_dir(path.parent)
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    tz_tag = tz_label(cfg.logging.timezone).lower().replace(" ", "_")
                    fh.write(clean_message(f"time_{tz_tag},symbol,side,close,pac_center,atr,trend") + "\n")
                    for r in rows:
                        fh.write(",".join(clean_message(cell) for cell in r) + "\n")
                ensure_private_file(path)
            except (ValueError, OSError) as exc:
                raise ConfigError(f"--csv: {exc}") from exc
            print(f"CSV ditulis: {path}")
    except BinanceError as exc:
        print(f"gagal: {exc}")
        return 1
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "pnl":
            return cmd_pnl(args)
        if args.command == "bookscan":
            return cmd_bookscan(args)
        if args.command == "signals":
            return cmd_signals(args)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\n[dihentikan] dibatalkan oleh pengguna (Ctrl+C)")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())

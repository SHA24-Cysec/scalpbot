# ScalpPullBack Bot (Binance USDT-M Futures)

Bot scalping untuk Binance Futures yang meniru logika indikator TradingView **"Scalping PullBack Tool R1.1 by JustUncleL"** (Pine v4), lalu menambahkan eksekusi order, manajemen risiko, dan dashboard terminal.

Ditulis dengan Python 3.11+ tanpa library trading besar: hanya `requests` untuk REST dan `websocket-client` untuk stream.

```
 SCALPTOOL BOT  PAC PULLBACK  |  TESTNET  SAFE
 WS:OK   0.1s  w:37  lat:61ms  IP: 103.28.14.77 (42s)  up:1h30m
 equity 48.62  avail 33.40  day +1.84 (+3.93%)  pos 2/3  risk 1.50%/trade  lev 10x
────────────────────────────────────────────────────────────────────────────────────
 POSISI TERBUKA
 SYMBOL     SIDE         QTY       ENTRY        MARK      R          SL          TP       PNL STATE
 SOLUSDT    LONG      0.7100     70.4200     70.8800  +1.12     70.5500     71.3000    0.3300 TRAIL
 DOGEUSDT   SHORT   860.0000    0.058420    0.058280  +0.62    0.058460    0.058200    0.1200 BE
```

Frame lengkap ada di `docs/dashboard-sample-plain.txt`.

---

## 1. Fitur

| Fitur | Ringkasan |
|---|---|
| Logika sinyal | PAC EMA(34) High/Low/Close, EMA ribbon 89/200/600, trend background, pullback recovery, Heikin Ashi opsional |
| Order | Market entry lewat `/fapi/v1/order`; SL `STOP_MARKET` + TP `TAKE_PROFIT_MARKET` lewat **Algo Order API** (`/fapi/v1/algoOrder`, `algoType=CONDITIONAL`, `triggerPrice`), reduce-only, selalu ada proteksi di exchange |
| SL / TP | Mode `atr`, `pac`, atau `percent`, dengan batas minimum dan maksimum jarak (bps) |
| Breakeven | Pindah SL ke titik BE yang sudah memasukkan fee 2 arah, slippage, dan buffer |
| Trailing | Trailing berbasis ATR setelah R tertentu, ratchet satu arah, throttle cancel-replace |
| Risiko | `risk_pct` per trade, batas notional, batas harian, cooldown per simbol |
| Orderbook | Filter imbalance bid/ask (top N level, berbobot) dan spread sebelum entry |
| Dashboard | Layar terminal alternatif (tanpa scroll), tahan resize, warna status |
| IP | Deteksi IP publik untuk whitelist API key, cache 10 menit |
| Log | Event log di layar + file JSONL + log rotasi |
| Bandwidth | Kline lewat WebSocket, mark price hanya untuk simbol berposisi, depth hanya saat sinyal |
| Multi simbol | Default 5 simbol hasil `top_volume`, 5m, satu posisi per simbol (bisa diubah) |
| Discovery | `top_volume` aktif sebagai default: simbol dipilih dari volume 24 jam, diperbarui tiap 1800 detik |
| Modal kecil | Auto bump ke minimum notional selama risiko masih di bawah `max_bump_factor` |

---

## 2. Peta indikator TradingView ke bot

| Pine v4 | Implementasi bot | Catatan |
|---|---|---|
| `ema(haClose, n)` | `EMA` dengan seed SMA seperti Pine | `indicators.EMA` |
| `pacC/pacU/pacL` | EMA close/high/low panjang `hi_lo_len` | 3 EMA terpisah |
| `TrendDirection` | `fastEMA > mediumEMA and pacL > mediumEMA` untuk up, kebalikannya untuk down | `indicators.trend_direction` |
| `pacExitU` | `haOpen < pacU and haClose > pacU and barssince(close<pacC) <= lookback` | `barssince` dihitung manual per simbol |
| `TradeDirection` | State machine 0 / +1 / -1 identik urutan ternary Pine | Sinyal hanya saat transisi 0 ke +-1 |
| `isRegularFractal` / `isBWFractal` | `fractal_top` / `fractal_bottom` (pivot di `high[2]`) | Dipakai untuk info, tidak memblokir sinyal |
| Bar color, background trend | Kolom `ZONE` dan `TREND` di dashboard | Di chart aslinya berupa warna candle/background |
| `UseHAcandles` | `strategy.use_ha` | Berlaku untuk EMA, PAC, dan warna bar |
| `Lookback` | `strategy.lookback` | Sama seperti input Pine |

Status kesesuaian Pine: **sinyal, indikator, dan state machine identik** dengan script asli. Dua tambahan dari sisi bot (bukan dari script) adalah **filter rezim** dan **filter orderbook**; keduanya hanya menyaring entry setelah sinyal terbentuk, dan tidak mengubah sinyal itu sendiri. Set `strategy.regime_min_slope_bps` 0 dan `orderbook.min_imbalance` 0 untuk perilaku murni script.

Paritas sinyal **diuji, bukan diklaim**. `tools/pine_reference.py` adalah transkripsi literal script Pine R1.1 (fungsi `ema`, `isRegularFractal`, `isBWFractal`, `TrendDirection`, `pacExitU/L`, `barssince`, state machine `TradeDirection`, urutan ternary apa adanya). `tests/test_pine_parity.py` membandingkan sinyal bot dengan transkripsi itu, bar per bar:

| Uji | Hasil |
|---|---|
| 22.500 bar 5m, 5 simbol | **0 selisih** setelah warmup |
| Data BTC 5m 2.000 bar (fixture) | 24 sinyal Pine = 24 sinyal bot, 1832/1832 bar cocok |
| `use_ha` false, `filterBW` true, lookback 1/2/3/5 | 0 selisih |
| Kombinasi PAC/EMA 21-55-144-377 dan 13-34-89-233 | 0 selisih |
| `slow_ema` diubah 600 ke 997 | sinyal identik setelah warmup (sesuai komentar script) |

Dua catatan kejujuran:

1. `haOpen` di `pacExitU/L` tidak diberi index di Pine. Saat dievaluasi live, artinya **haOpen bar yang sedang berjalan**. Bot memakai semantik yang sama. Bila dievaluasi hanya pada bar tertutup, hasilnya akan bergeser 8 sinyal dari 22.500 bar, jadi pilihan ini menentukan.
2. `isRegularFractal`/`isBWFractal` dan HH/LH/LL dihitung setiap bar tetapi **tidak dipakai** untuk sinyal, baik di script maupun di bot. Di script keduanya hanya menggambar bentuk; di bot tidak ditampilkan sebagai kolom ARMD.

Dua keputusan teknis yang disengaja:

1. Sinyal dievaluasi **hanya saat candle tutup**, bukan intrabar. Chart TradingView bisa memberi panah yang bergerak selama candle berjalan (repaint), bot tidak.
2. Fractal dan HH/HL/LL pada script asli memakai `high`/`low` chart, bukan Heikin Ashi. Perilaku itu dipertahankan: HC/EMA memakai HA, fractal memakai harga asli.

---

## 3. Instalasi

Butuh Python 3.11 atau lebih baru.

Linux/macOS:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
./run.sh check -c config.json
```

Windows (PowerShell, pakai Windows Terminal atau terminal lain yang mendukung ANSI):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
.\run.ps1 check -c config.json
```

Di `cmd.exe` pakai `run.bat check -c config.json`. Kalau perintah `python3` tidak ada di Windows, pakai `python -m scalpbot ...` atau `py -3 -m scalpbot ...`. Semua launcher melakukan `cd` ke folder repo supaya path `logs/` dan `config.json` relatif tetap benar.

Versi dependensi dipatok ke rilis terbaru PyPI saat paket ini dibuat (requests 2.34.2, websocket-client 1.9.2, dicek dari `pypi.org/pypi/<nama>/json` pada 2026-11-16). Sebelum install di PC lain, bandingkan isi `requirements.txt` dengan versi yang Anda inginkan; jangan menambah paket tanpa audit.

### Checklist sebelum dijalankan di PC lokal

1. `python -m scalpbot check -c config.json`. Ini memeriksa tipe dan batas semua config, koneksi REST, offset jam, kandidat `top_volume`, filter simbol, dan IP publik.
2. Samakan jam PC (NTP). Bot menyinkronkan offset ke server Binance saat start, tapi selisih lebih dari 60 detik tetap dicatat sebagai peringatan.
3. Pakai Windows Terminal atau terminal dengan VT processing. Kalau ANSI tidak bisa diaktifkan, dashboard otomatis mati dan log dialihkan ke stderr.
4. Bila jaringan memakai proxy, isi `net.proxy` dengan URL `http://`, `https://`, atau `socks5://`. Proxy yang sama dipakai REST dan WebSocket. SOCKS5 butuh PySocks untuk WebSocket; tanpanya koneksi WS langsung, jadi cek event `ws` bila status `DOWN`.
5. Jalankan dulu `--paper --paper-balance 50` dan `--no-dashboard` untuk melihat log tanpa layar alternatif.
6. Simpan API key di environment variable, bukan di config. `check` menampilkan IP yang perlu dimasukkan ke whitelist.

---

## 4. Setup testnet (default)

1. Buka https://testnet.binancefuture.com, login dengan akun Binance, saldo testnet diisi otomatis.
2. Scroll ke bagian API Key, buat key baru, salin key dan secret.
3. Set environment variable (jangan simpan di file config):

```bash
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
```

4. Verifikasi:

```bash
./run.sh check -c config.json
```

Perintah `check` menampilkan base URL, offset waktu server, kandidat `top_volume` beserta volumenya, filter simbol (tick, step, min notional), saldo, posisi terbuka, dan IP publik. Kalau API key memakai whitelist IP, daftarkan IP yang muncul di situ.

Testnet punya aturan minimum notional sendiri (BTCUSDT di testnet = 50 USDT, beberapa altcoin = 5 USDT). Bot membaca angka ini dari `exchangeInfo`, bukan hardcode.

Catatan URL testnet: konfigurasi bawaan memakai `https://testnet.binancefuture.com` dan `wss://fstream.binancefuture.com`, diverifikasi masih merespons dari workspace pada 2026-11-16. Dokumentasi Binance `General Info` yang diambil pada tanggal sama mencantumkan testnet baru `https://demo-fapi.binance.com` dan `wss://demo-fstream.binance.com`. Kalau URL lama sudah tidak tersedia dari jaringan Anda, ganti `api.base_url_testnet` dan `api.ws_testnet` sesuai dokumentasi tersebut, lalu jalankan `check` lagi.

---

## 5. Menjalankan

```bash
# mode paper: data live, simbol otomatis dari top_volume, tanpa API key
./run.sh run -c config.json --paper --paper-balance 50

# sinyal historis untuk dibandingkan dengan panah di TradingView
./run.sh signals -c config.json --symbol BTCUSDT --bars 1500 --csv docs/sinyal-btc.csv

# replay otomatis memakai daftar discovery, tanpa --symbol
./run.sh signals -c config.json --bars 1500

# testnet (order dikirim ke akun testnet)
./run.sh run -c config.json

# live: butuh dua langkah pengamanan
export SCALPBOT_LIVE_CONFIRM=YES-LIVE
./run.sh run -c config.json --live

# asal angka DAY di dashboard: PnL hari ini (hari exchange UTC) dari Binance, per tipe dan per simbol
./run.sh pnl -c config.json

# ukur orderbook universe: kenapa entry ditolak (spread / imbalance / snapshot basi)
./run.sh bookscan -c config.json --samples 6
```

Untuk memakai daftar simbol tetap, ubah `market.discovery` menjadi `static` di config. `market.symbols` lalu dipakai apa adanya dan tidak ada request ticker 24 jam.

Mode live hanya aktif jika `mode` di config bernilai `live`, flag `--live` dipakai, **dan** env `SCALPBOT_LIVE_CONFIRM=YES-LIVE` diset (atau mengetik `LIVE` saat diminta di terminal). Tanpa itu bot menolak jalan.

Opsi lain: `--no-dashboard` untuk log baris-per-baris (cocok untuk `screen`/`nohup`), `--log-level DEBUG`, `--paper` untuk memaksa mode simulasi.

---

## 6. Konfigurasi

Semua di `config.json`. Kunci yang tidak dikenal akan ditolak, bukan diabaikan.

### market

| Kunci | Default | Arti |
|---|---|---|
| `symbols` | 5 simbol | Fallback bila `top_volume` tidak menghasilkan kandidat, dan daftar utama saat `static`. Bisa ditulis singkat (`BTC` menjadi `BTCUSDT`). Wajib PERPETUAL, TRADING, quote dan margin USDT |
| `interval` | `5m` | `1m` sampai `1d` |
| `warmup_bars` | 1000 | Bar historis untuk pemanasan EMA(600); maksimal 1500 (batas Binance) |
| `discovery` | `top_volume` | Bawaan: pilih otomatis. Set `static` untuk memakai `symbols` apa adanya |
| `discovery_top_n` | 5 | Jumlah simbol yang dipantau saat `top_volume` (1..25, di atas 10 memunculkan peringatan) |
| `discovery_refresh_s` | 1800 | Interval pemilihan ulang, minimal 300 detik |
| `discovery_quote_volume_min` | 50000000 | Volume 24 jam minimum dalam USDT |
| `discovery_exclude` | `[]` | Simbol yang tidak boleh dipilih |
| `discovery_require_affordable` | true | Buang simbol yang minimum notional-nya tidak muat modal |

Semua kunci config divalidasi tipenya, termasuk `bool` yang tidak boleh diisi `1`, angka `NaN`/`Infinity`, dan URL yang bukan `https`/`wss`.

### Discovery simbol (`top_volume`)

Ini konfigurasi bawaan. Bot memilih simbol sendiri; `market.symbols` tetap diisi sebagai fallback kalau endpoint ticker gagal atau semua kandidat tersaring.

```json
"market": {
  "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
  "interval": "5m",
  "warmup_bars": 1000,
  "discovery": "top_volume",
  "discovery_top_n": 5,
  "discovery_refresh_s": 1800,
  "discovery_quote_volume_min": 50000000,
  "discovery_exclude": [],
  "discovery_require_affordable": true
}
```

Cara kerjanya:

1. Saat start, bot mengambil `/fapi/v1/ticker/24hr` (satu request, weight 40) dan `/fapi/v1/exchangeInfo`, lalu menyaring: hanya PERPETUAL + TRADING, quote asset USDT, margin asset USDT, simbol tidak masuk `discovery_exclude`, volume 24 jam di atas batas, dan (bila `discovery_require_affordable`) minimum notional simbol tidak melebihi `equity x leverage x max_notional_frac_equity`. Kalau hasilnya kosong, bot memakai `market.symbols` dan mencatat peringatan di event log.
2. Kandidat diurutkan berdasarkan `quoteVolume` menurun, diambil `discovery_top_n` teratas.
3. Setiap simbol baru menjalani warmup 1000 bar, leverage dan margin type diset, lalu kline-nya di-`SUBSCRIBE` ke koneksi WebSocket yang sudah jalan. Tidak perlu reconnect.
4. Setiap `discovery_refresh_s` daftar dihitung ulang. Simbol baru masuk otomatis, simbol yang keluar di-`UNSUBSCRIBE` dan dilepas dari pemantauan.
5. Kalau simbol yang keluar masih punya posisi terbuka, simbol itu **ditahan** sampai posisinya tutup, baru dilepas. Bot mencatat alasannya di event log.

Verifikasi tanpa menjalankan bot:

```bash
./run.sh check -c config.json
```

Bagian `kandidat` menampilkan simbol terpilih beserta volume, misalnya `ETHUSDT(132029M), BCHUSDT(126645M), XRPUSDT(126340M)`.

Catatan penting:

- Pemilihan ulang memakai data volume 24 jam, jadi daftar bisa berubah antar siklus. Untuk hasil yang bisa direproduksi, set `market.discovery` ke `static`; `market.symbols` lalu dipakai apa adanya.
- Simbol baru butuh waktu hangat sebelum bisa memberi sinyal (sekitar 600 bar untuk EMA 600). Selama itu nilainya muncul di dashboard dengan status belum siap.
- `top_volume` tetap memperhatikan modal: dengan saldo 50 USDT, simbol yang minimum notional-nya besar akan dibuang walaupun volumenya paling besar.
- Isi `discovery_exclude` bila ada simbol yang tidak boleh dipilih, misalnya `["BTCUSDT"]`.
- Jika satu siklus discovery tidak menghasilkan simbol (misalnya semua tersaring batas volume atau modal), daftar lama dipertahankan dan event log memberi peringatan.

### strategy

`hi_lo_len` 34, `fast_ema` 89, `medium_ema` 200, `slow_ema` 600, `lookback` 3, `filter_bw_fractals` false, `use_ha` true.

Filter rezim tren:

| Kunci | Default | Arti |
|---|---|---|
| `regime_slope_bars` | 200 | Jarak (bar) untuk mengukur slope `slow_ema` |
| `regime_min_slope_bps` | 100.0 | LONG hanya diterima bila slope >= nilai ini, SHORT bila <= -nilai ini. **0 = filter mati** |

Filter rezim **aktif secara default** (100 bps). Ini tambahan dari bot, bukan bagian indikator TradingView: script Pine menyatakan `slowEMA(600) is not used in this Algo`. Untuk perilaku 100% mengikuti chart, set `regime_min_slope_bps` ke `0`. Perintah `signals` sudah ikut menyaring; pakai `--show-blocked` untuk melihat sinyal mentah beserta tandanya.

Filter ini memakai `slow_ema` yang sebelumnya dihitung tapi tidak dipakai. Efeknya: entry dikurangi ~85%, tetapi ekspektasi naik dari +0,06 R ke +0,155 R (lihat bagian 14). Karena itu warmup otomatis naik menjadi `slow_ema + regime_slope_bars + 8` (808 bar) saat filter aktif.

Untuk scalp lebih rapat, contoh di dokumen Pine: PAC 21 dengan EMA 55/144/377, atau PAC 13 dengan 34/89/233.

### risk

| Kunci | Default | Arti |
|---|---|---|
| `risk_pct` | 1.5 | Risiko per trade terhadap equity, **0.01–100**. Nilai > batas notional akan dipotong (lihat "Batas risiko efektif") |
| `leverage` | 10 | Leverage yang diset ke exchange |
| `margin_type` | ISOLATED | `ISOLATED` atau `CROSSED` |
| `max_open_positions` | 3 | Total posisi bersamaan |
| `max_open_per_symbol` | 1 | Jumlah posisi bersamaan per simbol, bisa diubah sampai `max_open_positions` |
| `max_notional_frac_equity` | 1.0 | Batas notional = equity x leverage x nilai ini |
| `min_notional_bump` | true | Naikkan qty ke minimum exchange bila perlu |
| `max_bump_factor` | 2.0 | Bump hanya boleh sampai risiko 2x dari target |
| `daily_loss_limit_pct` | 5.0 | Stop entry kalau rugi harian tercapai. Dasar hitungannya diambil dari `/fapi/v1/income` (REALIZED_PNL + COMMISSION), jadi tetap berlaku setelah bot di-restart |
| `sl_mode` | atr | `atr`, `pac`, atau `percent`. Lihat tabel di bawah |
| `atr_len` | 14 | Panjang ATR (Wilder), dipakai mode `atr` |
| `sl_atr_mult` | 5.0 | Mode `atr`: jarak SL = ATR x nilai ini. Di 5m median ~94 bps |
| `sl_percent` | 0.35 | Mode `percent`: jarak SL = harga x nilai ini / 100 |
| `sl_min_bps` / `sl_max_bps` | 8 / 250 | Batas jarak SL dalam basis poin, berlaku untuk **ketiga** mode |

Tiga mode SL, semuanya tetap dijepit oleh `sl_min_bps` dan `sl_max_bps` lalu dibulatkan ke tick size:

| `sl_mode` | Rumus | Karakter | Hasil backtest 312 hari (expR / PF) |
|---|---|---|---|
| `atr` (default) | `entry -/+ ATR x sl_atr_mult` | Menyesuaikan volatilitas. Di 5m median 93,8 bps (p10 44,8, p90 148,3) | **+0,155 / 1,26** |
| `pac` | `min(pac_lower, entry - 0,3 x ATR)` untuk LONG, `max(pac_upper, entry + 0,3 x ATR)` untuk SHORT | SL di balik batas Price Action Channel. Median 34,3 bps | -0,126 / 0,99 |
| `percent` | `entry x (1 -/+ sl_percent/100)` | Jarak tetap, tidak peduli volatilitas. 0,35% selalu ~35 bps | -0,115 / 0,84 (dengan 0,80% jadi +0,081 / 1,16) |

Kenapa `atr` menang di data ini: strategi sudah menyaring rezim, jadi trade yang lolos cenderung bergerak jauh. SL 34-35 bps terlalu sempit untuk ayunan 5m, sehingga SL kena sebelum TP 4R sempat tercapai. `percent` 0,35% juga tidak menyesuaikan volatilitas: di koin tenang terlalu lebar, di koin liar terlalu sempit.

`pac` punya satu kuirk: rumusnya memakai `min`/`max`, dan bila harga sudah jauh dari PAC (misal entry 50 bps di atas `pac_lower`), hasilnya bisa hanya 15 bps. Nilai itu bukan pilihan, tapi konsekuensi rumus.
| `tp_rr` | 4.0 | Take profit = R x nilai ini |
| `be_trigger_r` | 1.0 | Pindah SL ke BE setelah profit 1R |
| `be_buffer_bps` | 2.0 | Buffer di atas titik BE |
| `trail_trigger_r` | 2.5 | Trailing mulai setelah 2.5R. Lebih lambat supaya TP 4R masih bisa tercapai |
| `trail_atr_mult` | 1.2 | Jarak trailing = ATR x nilai ini |
| `trail_step_bps` | 5.0 | Minimal pergerakan SL sebelum cancel-replace |
| `fee_rate` | 0.0004 | Fee taker futures 0.04% |
| `cooldown_bars` | 1 | Jeda bar sebelum boleh entry lagi di simbol sama |
| `max_hold_minutes` | 0 | 0 berarti tanpa batas waktu tahan |
| `adopt_existing` | true | Adopsi posisi yang sudah terbuka di exchange saat bot start (lihat bagian "Restart saat ada posisi terbuka") |

### Formulasi yang dipakai

- **SL**: `entry -/+ ATR x sl_atr_mult`, dijepit ke `[sl_min_bps, sl_max_bps]`, dibulatkan ke tick size (ke arah yang aman).
- **TP**: `entry +/- (jarak SL) x tp_rr`.
- **BE + biaya**: `entry x (1 + 2 x fee_rate + slippage + be_buffer_bps)` untuk long, kebalikannya untuk short. Fee dua sisi dan slippage ikut dihitung supaya BE benar-benar impas.
- **Trailing**: `harga -/+ ATR x trail_atr_mult`, hanya bergerak satu arah (ratchet), tidak pernah lebih buruk dari BE, dan hanya dikirim bila perubahan melebihi `trail_step_bps` supaya tidak membanjiri API.
- **PnL bersih**: PnL kotor dikurangi fee entry dan exit pada harga eksekusi.

### Batas risiko efektif (penting untuk modal kecil)

`risk_pct` boleh diisi 1 sampai 100, tetapi **angka yang benar-benar terpasang** ditentukan dua pembatas lain:

```
notional maks   = equity × leverage × min(max_notional_frac_equity, 1.0)
guard margin    = margin yang dibutuhkan harus <= 98% dari saldo tersedia
risiko efektif  = notional × (SL bps / 10000) / equity
```

Karena itu, risiko maksimum yang bisa dicapai kira-kira:

```
risk_maks ≈ leverage × 0.98 × (SL bps / 10000) × 100%
```

Contoh nyata dengan leverage 10 dan `max_notional_frac_equity` 1.0:

| SL | `risk_pct` | notional | risiko **benar-benar** terpasang |
|---|---|---|---|
| 94 bps | 1.5% | 128 USDT | 1,5% (utuh) |
| 94 bps | 10% | 960 USDT | 9,0% (utuh) |
| 94 bps | 100% | 960 USDT | **9,0%** — dipotong, bukan 100% |
| 250 bps | 100% | 980 USDT | **24,5%** — dipotong |

Bot tidak lagi memotong diam-diam. Kalau qty dibatasi notional:

- `Sizing.capped = true` dan `Sizing.reason` memuat risiko yang terpasang.
- Event **WARNING** muncul sekali per simbol: `risiko terpasang X% equity (Y USDT), bukan Z% yang diminta`.
- Log `OPEN` menulis risiko efektif dan menandai `DIPOTONG`.
- `python -m scalpbot check` menampilkan baris `batas` berisi risiko maksimum yang bisa dicapai.

Untuk benar-benar mengeksekusi `risk_pct` besar, `leverage` yang harus dinaikkan (bukan `risk_pct`), misalnya risiko 100% dengan SL 250 bps butuh `leverage` sekitar 40.

### Modal kecil (di bawah 50 USDT)

Minimum notional Binance Futures adalah 5 USDT, jadi yang menentukan bukan `risk_pct`, melainkan `min_notional_bump`. Hasil pengukuran pada DOGE 0,06 dengan SL 94 bps:

| Equity | `risk_pct` | Notional hasil | Status |
|---|---|---|---|
| 10 USDT | 1% | 10,62 USDT | jalan |
| 10 USDT | 1,5% | 15,90 USDT | jalan |
| 10 USDT | 5% | 53,16 USDT | jalan |
| 10 USDT | 100% | 99,96 USDT | jalan, tapi risiko efektif ±9% (dipotong batas notional) |
| 20 USDT | 5% | 106,38 USDT | jalan |
| 50 USDT | 1,5% | 79,74 USDT | jalan |

Artinya modal 10 USDT sudah cukup; `risk_pct` naik sampai batas notional. Bila notional hasil hitung di bawah 5 USDT, `min_notional_bump` menaikkan qty ke batas minimum selama risiko hasil bump tidak melebihi `max_bump_factor` (2x) dari risiko yang diminta, dan **risiko aktual dilaporkan** di log `OPEN` sebagai `BUMP`.

### orderbook, execution, net, dashboard, logging

| Bagian | Kunci | Default | Arti |
|---|---|---|---|
| api | `timeout_s` | 10 | Timeout REST per request, 0..120 detik |
| api | `max_retries` | 3 | Batas atas percobaan GET saat 429/5xx atau error jaringan |
| api | `recv_window_ms` | 5000 | Jendela validitas request bertanda tangan |
| api | `base_url_testnet` / `base_url_live` | testnet / fapi | Wajib `https` |
| api | `ws_testnet` / `ws_live` | fstream | Wajib `wss` |
| orderbook | `min_imbalance` | 0.05 | Long butuh bid volume 5% lebih besar dari ask pada top N level. Default lama 0.15 memblokir ~63% entry |
| orderbook | `periodic_snapshot_s` | 0 | 0 berarti depth hanya diambil saat ada sinyal |
| orderbook | `max_age_s` | 15 | Snapshot lebih tua dari ini ditolak saat entry |
| execution | `working_type` | MARK_PRICE | Pemicu SL/TP: mark price atau harga kontrak |
| execution | `position_poll_s` | 5 | Interval polling saldo |
| execution | `reconcile_s` | 30 | Interval rekonsiliasi posisi |
| execution | `cancel_replace_min_s` | 2.0 | Jeda minimal antar cancel-replace SL |
| execution | `max_slippage_bps` | 25.0 | Spread maksimum saat entry |
| net | `ip_check` | true | Deteksi IP publik |
| net | `proxy` | "" | URL `http`/`https`/`socks5`, dipakai REST dan WS |
| dashboard | `min_width` / `min_height` | 72 / 22 | Di bawah ini tampil pesan perbesar terminal |
| dashboard | `log_lines` | 8 | Baris event log, 1..50 |
| dashboard | `history_lines` | 5 | Baris panel riwayat entry, 0..20 (0 = panel disembunyikan) |
| logging | `max_bytes` / `backups` | 2000000 / 3 | Rotasi file log |
| logging | `timezone` | "WIB" | Zona waktu **tampilan** (log, `pnl`, `signals`, CSV) dan hari bot. Contoh: `WIB`, `WITA`, `WIT`, `Asia/Jakarta`, `UTC` |
| logging | `persist_state` | true | Simpan cooldown + riwayat entry ke disk supaya bertahan setelah restart |
| logging | `state_file` | logs/state.json | Path dasar berkas state; folder mode disisipkan otomatis |
| logging | `file` / `events_file` | logs/... | Path dasar relatif terhadap folder repo; nama folder mode disisipkan otomatis |
| logging | `mode_subdir` | true | `logs/paper`, `logs/testnet`, `logs/live`; set `false` untuk satu file bersama |

---

### Apakah ada filter yang menolak sideways?

Ada, `strategy.regime_min_slope_bps`, tapi ketahuilah apa yang sebenarnya diukurnya. Filter itu mengukur **arah** EMA lambat, bukan **kualitas** gerakan.

Diukur pada 312 hari: efisiensi pergerakan pasar 5m di 5 simbol ini hanya **0,06** (median, skala 0 sampai 1). Artinya pasar hampir selalu choppy, bukan sedang tren.

| | n sinyal | efisiensi p50 |
|---|---|---|
| Lolos filter rezim | 2.153 | 0,094 |
| Ditolak | 5.253 | 0,069 |

Jadi filter menolak sinyal yang lebih choppy, tapi yang lolos pun masih choppy. Kalau Anda ingin benar-benar hanya entry saat tren kuat, ada gerbang tambahan yang diuji: efisiensi pergerakan >= 0,08 menaikkan PF dari 1,26 ke 1,43 dan expR dari +0,155 ke +0,208 (639 trade). Uji ketahanan: 4 dari 5 simbol membaik, kedua paruh waktu membaik. Tapi kurvanya naik-turun (puncak di 0,08, lalu turun), jadi jangan anggap angka itu keramat.

Gerbang efisiensi **belum dipasang di bot** karena bukan bagian indikator Pine dan akan membuat sinyal makin jauh dari chart. Rinciannya di `docs/backtest-parameter.md` bagian 7.

### Kalau filter orderbook dimatikan

`orderbook.enabled: false` mematikan **ketiga** gerbang sekaligus (age, spread, imbalance). Bisa juga dimatikan sebagian: `min_imbalance: 0` mematikan gerbang imbalance saja, `max_spread_bps: 0` mematikan gerbang spread.

Pengukuran nyata (20 sampel per simbol, jeda 5 detik, depth 20) menunjukkan imbalance **tidak acak**: ia bisa terkunci ke satu arah untuk waktu lama.

| Simbol | imbalance p50 | LONG lolos | SHORT lolos |
|---|---|---|---|
| BTCUSDT | **+0,57** | 20/20 | **0/20** |
| ETHUSDT | -0,47 | 5/20 | 15/20 |
| BCHUSDT | -0,06 | 1/20 | 12/20 |
| XRPUSDT | +0,08 | 12/20 | 3/20 |
| DOGEUSDT | -0,10 | 7/20 | 11/20 |

Di BTCUSDT, imbalance berkisar +0,16 sampai +0,92 di **20 dari 20** sampel. Artinya selama kondisi itu bertahan, **semua SHORT di BTC ditolak** bukan karena pasar menolak, tapi karena struktur depth-nya memang timpang.

Dampaknya ke P&L (312 hari, filter rezim aktif), bila satu sisi diblokir permanen:

| Simbol | Sisi diblokir | Net semua | Net setelah diblokir | Selisih |
|---|---|---|---|---|
| BTCUSDT | SHORT (seperti terukur) | +4,0 | **-8,9** | -12,9 |
| BCHUSDT | LONG | +38,5 | +32,3 | -6,1 |
| TOTAL | | +137,5 | +118,4 | **-19,1** |

Jadi memblokir satu sisi dapat mengubah simbol yang tadinya untung menjadi rugi, hanya karena struktur depth, bukan karena sinyalnya buruk.

**Kesimpulan praktis:**

- Gerbang `max_age_s` **jangan** dimatikan. Itu mencegah bot entry memakai harga basi, dan tidak mengurangi jumlah entry sama sekali (hanya menolak snapshot tua).
- Gerbang `min_imbalance` paling aman dimatikan (`0`) atau dipertahankan sangat longgar (`0.05`). Backtest tidak bisa memvalidasi gerbang ini karena tidak memodelkan orderbook; yang bisa diukur hanya berapa banyak entry yang hilang.
- Gerbang `max_spread_bps` 5,0 praktis tidak pernah aktif pada 5 simbol ini (spread p50 0,47 bps, maks 4,12 bps). Ia berguna sebagai jaring pengaman saat pasar kacau, jadi biarkan.

### Kenapa entry sering ditolak filter orderbook

Filter orderbook punya tiga gerbang, dan semuanya harus lolos sebelum order dikirim:

| Gerbang | Default | Arti |
|---|---|---|
| `max_spread_bps` | 5.0 | spread bid/ask lebih lebar dari 5 bps ditolak |
| `min_imbalance` | 0.05 | LONG butuh `(bid_vol - ask_vol) / total >= +0.05`; SHORT butuh `<= -0.05`. Default lama 0.15 memblokir ~63% entry |
| `max_age_s` | 15 | snapshot depth lebih tua dari 15 detik ditolak |

Gerbang `min_imbalance` yang paling sering memblokir. Alasannya: kedalaman pada 20 level teratas di pasar normal hampir selalu berimbang (imbalance sekitar 0.00 sampai 0.10). Menuntut 0.15 berarti menunggu momen ketika satu sisi book benar-benar dua kali lebih tebal, dan momen itu jarang.

Contoh pengukuran nyata dari workspace ini (12 sampel per simbol, depth 20):

| Simbol | median \|imbalance\| | maks \|imbalance\| | lolos 0.15 |
|---|---|---|---|
| BTCUSDT | 0.387 | 0.660 | 10/12 |
| ETHUSDT | 0.650 | 0.735 | 12/12 |
| BCHUSDT | 0.033 | 0.067 | 0/12 |
| XRPUSDT | 0.001 | 0.089 | 0/12 |
| DOGEUSDT | 0.082 | 0.119 | 0/12 |

Tiga dari lima simbol besar tidak pernah menyentuh ambang sama sekali. Kalau bot kebetulan memilih simbol seperti itu, semua sinyal akan ditolak dan terlihat seperti "bot tidak pernah entry".

Mengukurnya di universe Anda sendiri:

```bash
python -m scalpbot bookscan -c config.json --samples 6
```

Keluarannya menunjukkan spread, imbalance, dan berapa percobaan yang lolos, plus penyebab dominan per simbol. Bot juga mencatat sendiri: setelah 5 penolakan, muncul satu `WARNING` di event log yang menyebut ambang penyebabnya, dan ringkasan sesi menampilkan baris `tolak entry : imbalance 7, spread 1 (orderbook)`.

Pilihan konfigurasi:

| Ingin | Set |
|---|---|
| Filter imbalance tidak memblokir, spread tetap dijaga | `orderbook.min_imbalance: 0.0` |
| Hanya blokir saat book sangat berat sebelah | `orderbook.min_imbalance: 0.35` |
| Matikan seluruh filter orderbook | `orderbook.enabled: false` |
| Longgar pada simbol tipis seperti DOGE/XRP | `orderbook.min_imbalance: 0.05` |

---

## 7. Ramah modal kecil (di bawah 50 dolar)

- Ukuran posisi selalu dihitung dari risiko, bukan dari "uang yang ada". Dengan modal 50 USDT, risiko 1.5%, dan SL 50 bps, notional awal hanya sekitar 15 USDT. Kalau di bawah minimum exchange, bot menaikkan qty ke minimum notional **hanya** jika risiko hasil bump masih di bawah `max_bump_factor` kali target. Kalau tidak, trade dilewati dan alasannya dicatat di event log.
- `max_notional_frac_equity` mencegah bukan hanya risiko tetapi juga margin terpakai.
- Minimum notional tiap simbol berbeda: di live, altcoin seperti SOL/XRP/DOGE biasanya 5 USDT, BTCUSDT jauh lebih besar. Dengan modal kecil, jalankan `./run.sh check` lalu pilih simbol yang minimumnya terjangkau. Bot otomatis menolak simbol yang tidak muat, bukan memaksakan order.
- Fee adalah musuh utama scalping: dengan fee taker 0.04% bolak-balik, target 1.5R pada SL 50 bps baru memberi sekitar 0.65R bersih. Naikkan `tp_rr` atau kurangi frekuensi trade kalau hasil bersih terlalu tipis.

---

## 8. Hemat bandwidth dan rate limit

| Sumber | Cara pakai |
|---|---|
| Kline | Satu koneksi WebSocket combined untuk semua simbol hasil discovery, bukan polling REST. Ini penyumbang ~94% bandwidth (≈179 MB/hari untuk 5 simbol) |
| Mark price | Stream `@markPrice@1s` hanya di-subscribe saat simbol punya posisi, lalu di-unsubscribe |
| User data | WebSocket listenKey, keepalive tiap 30 menit |
| Depth | REST `limit=20` hanya saat sinyal (weight 2), atau sesuai `periodic_snapshot_s` |
| Balance/posisi | `/fapi/v3/balance` dan `/fapi/v3/positionRisk` dengan interval 5 detik saat ada posisi |
| Open orders | Query per simbol (weight 1), hanya untuk simbol berposisi |
| IP publik | Satu request tiap 10 menit |
| Dashboard | Menulis ke terminal lokal, tidak ada trafik jaringan |

### Berapa bandwidth yang dipakai (diukur, bukan perkiraan)

Angka berikut dari pengukuran nyata ke testnet Binance, 5 simbol, interval 5m:

| Sumber | Per hari |
|---|---|
| WebSocket kline, 5 simbol (≈350 byte/frame, ≈1 frame/detik/simbol) | **178,8 MB** |
| `ticker/24hr` untuk discovery tiap 30 menit (48x, 232 KB/response) | 11,4 MB |
| Snapshot depth saat entry (~3 kali/hari, ~1 KB) | 0,03 MB |
| **Total stabil** | **≈190 MB/hari** (≈2 KB/s, 5,7 GB/bulan) |

Saat start sekali saja: warmup kline 5 simbol ≈0,7 MB, `exchangeInfo` 884 KB per panggilan, discovery pertama 232 KB.

Dua hal penting dari pengukuran ini:

1. **Interval tidak menghemat bandwidth.** Binance tetap mengirim ~1 frame per detik per simbol walau interval-nya 1h. Diukur: 5m = 29,6 MB/hari/simbol, 15m = 30,0, 1h = 51,3. Yang berkurang hanya frekuensi sinyal, bukan trafik.
2. **Jumlah simbol berpengaruh linear.** 5 simbol ≈ 4,2x dari 1 simbol. Bila kuota internet terbatas, kurangi `discovery_top_n`.

Saat ada posisi terbuka, polling REST menambah sekitar 2 MB/jam per posisi (`position_poll_s` 5 detik). Tanpa posisi, tidak ada polling sama sekali.

Cara menghemat: `market.discovery` ke `static` (hemat 11,4 MB/hari), turunkan `discovery_top_n`, atau jalankan bot di VPS/PC yang selalu menyala supaya tidak perlu start berulang.

Kolom `w:` di dashboard adalah `X-MBX-USED-WEIGHT-1M`; batasnya dibaca dari `rateLimits` di `exchangeInfo`, bukan hardcode. Kalau mendekati batas, kurangi `periodic_snapshot_s` atau jumlah simbol. Jika WebSocket mati lebih dari 90 detik, bot pindah sementara ke REST 3 kline terakhir per simbol sampai koneksi pulih, dan hanya bar yang sudah tutup yang dimasukkan ke strategi. Update kline yang belum tutup dibatasi maksimal satu per detik per simbol supaya antrean event tidak membengkak.

Perintah `SUBSCRIBE` dan `UNSUBSCRIBE` dikirim lewat koneksi WebSocket yang sama saat daftar simbol berubah. Balasan server dicatat di event log level DEBUG sebagai `ack stream`.

---

## 9. Dashboard terminal

- Memakai alternate screen (`ESC[?1049h`), jadi layar lama tetap utuh setelah keluar dan tidak mengotori scrollback.
- Kursor disembunyikan selama render dan dimunculkan kembali di akhir.
- Ukuran diambil ulang setiap frame dari `shutil.get_terminal_size`, jadi resize tidak merusak layout. Bagian event log yang dipotong duluan kalau tinggi layar kurang.
- Setiap frame diakhiri `ESC[J` supaya sisa baris dari frame sebelumnya hilang saat jumlah posisi atau simbol berkurang.
- Di Windows, bot mengaktifkan VT processing. Kalau tidak didukung, dashboard mati sendiri dan log tampil biasa. `SIGWINCH` tidak ada di Windows, jadi penanganannya dilewati tanpa error.
- Setiap baris dipotong sesuai lebar terminal dengan menghitung karakter yang terlihat (kode warna ANSI diabaikan), sehingga garis tidak terlipat.
- Sinyal `SIGWINCH` memaksa render ulang segera.
- `Ctrl+C` keluar rapi: WebSocket ditutup, listenKey dihapus, ringkasan sesi dicetak. Posisi yang masih terbuka tetap punya SL/TP di exchange.

### Tata letak

Header empat baris: identitas dan mode, status jaringan, akun, lalu `UNIVERSE` yang menampilkan mode discovery, jumlah simbol, interval refresh, dan batas volume.

Di bawahnya panel simbol dikelompokkan menurut kesiapan, bukan diurutkan abjad atau peringkat volume:

| Kelompok | Arti |
|---|---|
| `SIAP EKSEKUSI` | sedang arm, syarat pullback dan tren sudah terpenuhi |
| `DEKAT PAC` | tren sudah terbentuk dan harga menempel pita, jarak di bawah setengah ATR |
| `TREN JELAS` | arah tren terbentuk tapi harga masih jauh dari PAC |
| `FLAT` | tren belum terbentuk, sinyal tidak mungkin muncul |

Di dalam kelompok, baris diurutkan berdasarkan jarak absolut ke PAC. Kolom yang ditampilkan:

| Kolom | Arti |
|---|---|
| `SIMB` | nama simbol tanpa akhiran USDT |
| `PRICE` | harga terakhir |
| `TREN` | `UP`, `DOWN`, atau `FLAT` dari klasifikasi EMA |
| `PAC` | meter posisi harga terhadap PAC, skala tetap -3% sampai +3%, garis tengah `┼` adalah PAC |
| `D%` | jarak ke PAC dalam persen, untuk pembacaan cepat |
| `ZN` | `abv`, `blw`, atau `in`, posisi terhadap pita PAC |
| `SIG` | `LONG` atau `SHRT` saat arm, atau sinyal terakhir beserta umurnya |

Warna meter: merah di bawah PAC, biru di atas PAC, kuning saat jarak di bawah setengah ATR.

Pada terminal minimal 108 kolom, tampilan otomatis dibagi dua panel: simbol di kiri, posisi terbuka, riwayat entry, dan event log di kanan. Di bawah 108 kolom, keempat panel ditumpuk vertikal. Tinggi tiap panel dihitung dari sisa layar: panel simbol dan event log mendapat jatah minimal lebih dulu, riwayat entry dipotong lebih awal kalau layar sempit. Baris yang tidak muat diringkas menjadi `+N simbol lain`.

Saat belum ada posisi, panel posisi menampilkan tiga simbol terdekat ke PAC sebagai petunjuk, bukan sekadar tulisan kosong.

### Riwayat entry

Panel `RIWAYAT ENTRY` menampilkan beberapa kejadian terakhir dari sesi berjalan, bukan hasil backtest:

| Kolom | Arti |
|---|---|
| jam | waktu kejadian (`HH:MM`) |
| simbol | nama simbol tanpa akhiran USDT |
| `L`/`S` | sisi posisi |
| status | `OPEN` (entry terisi), `TP`, `SL`, `BE`, `TRAIL`, `TIMEOUT`, atau `TOLAK` (sinyal dibatalkan filter) |
| harga | harga fill entry/exit; `-` untuk penolakan filter rezim yang terjadi sebelum harga dihitung |
| detail | `R=xxbps` saat entry, `+x.xx$ ±x.xxR` saat posisi ditutup, `book: <alasan>` untuk tolak orderbook, `rezim` untuk tolak filter tren |

Warna: sian `OPEN`, hijau `TP`/`TRAIL`, merah `SL`, kuning `BE`/`TIMEOUT`/lain, abu `TOLAK`.

Batas dan sifatnya, supaya tidak salah tafsir:

- Hanya **di memori** proses yang sedang berjalan. Restart bot mengosongkan panel; riwayat jangka panjang tetap hanya ada di `logs/<mode>/events.jsonl` (baris `OPEN`/`CLOSE`) yang ikut rotasi.
- Kapasitas 200 baris terakhir, panel menampilkan `dashboard.history_lines` baris terbaru.
- Penolakan filter yang sama (simbol + alasan sama) dalam 300 detik digabung jadi satu baris dengan akhiran `xN`, supaya panel tidak tenggelam oleh tolak rezim yang berulang tiap bar. Angka `N` adalah jumlah penolakan yang diwakili baris itu, bukan jumlah sinyal yang berbeda.
- Baris `TOLAK` ikut tercatat meski tidak ada order yang dikirim, jadi panel ini bisa dipakai melihat **kenapa bot tidak entry**, bukan hanya apa yang sudah dieksekusi.

Frame contoh: `docs/dashboard-sample-wide-plain.txt` (150 kolom) dan `docs/dashboard-sample-standard-plain.txt` (120 kolom).

---

## 10. Event log

- Di layar: 8 baris terakhir (bisa diatur `dashboard.log_lines`).
- File JSONL: satu event per baris berisi `ts`, `iso`, `level`, `source`, `message`. Rotasi otomatis saat melewati `logging.max_bytes` dan hasil rotasi tetap di folder mode yang sama.
- Log teknis: `RotatingFileHandler` dengan prefix `[PAPER]`, `[TESTNET]`, atau `[LIVE]` di setiap baris. Berkas ini hanya menampung baris sesi (`mulai` dan `selesai` beserta equity, jumlah trade, dan error) plus warning/debug internal. Aktivitas trading tetap ada di berkas event, jadi tidak ada baris ganda.
- API key dan secret disaring sebelum ditulis ke log.

Berkas dipisah per mode sejak versi ini. `logging.file` dan `logging.events_file` hanya path dasar; nama folder mode disisipkan di antaranya, jadi `logs/scalpbot.log` menjadi:

| Mode | Log teknis | Log event |
|---|---|---|
| paper | `logs/paper/scalpbot.log` | `logs/paper/events.jsonl` |
| testnet | `logs/testnet/scalpbot.log` | `logs/testnet/events.jsonl` |
| live | `logs/live/scalpbot.log` | `logs/live/events.jsonl` |

Folder dibuat otomatis saat start. `check` mencetak path yang akan dipakai, dan ringkasan sesi mencetak path yang benar-benar dipakai. Set `logging.mode_subdir` ke `false` bila ingin kembali ke satu file bersama. Nama folder selalu diambil dari `mode` yang sedang aktif, jadi paper tidak mungkin menulis ke berkas testnet atau live.

Contoh alur satu trade:

```
INFO  signal  SHORT DOGEUSDT @0.05842 PAC=0.05855 ATR=0.000146 trend=-1
INFO  trade   OPEN SHORT DOGEUSDT qty=860 @0.05842 SL=0.05864 TP=0.05820 risk=0.73$ R=38bps imb=-0.12
INFO  manage  DOGEUSDT BE dipasang @0.05838 (R=+0.55)
INFO  manage  DOGEUSDT trailing SL -> 0.05831 (peak R=+1.20)
INFO  trade   CLOSE SHORT DOGEUSDT TRAIL @0.05831 PnL +0.83$ (+1.10R)
```

### Restart saat ada posisi terbuka

Mematikan bot (Ctrl+C) **tidak** menutup posisi dan **tidak** membatalkan SL/TP. Order proteksi hidup di server Binance, jadi SL/TP tetap tereksekusi walau PC mati.

Yang dulu menjadi masalah: bot tidak menyimpan posisi ke disk, sehingga setelah restart bot mengira dirinya flat (`POS 0/3`), posisi lama tidak di-trail, tidak dipindah ke BE, tidak dihitung ke `max_open_positions`, dan tidak punya batas waktu.

Sekarang posisi lama **diadopsi saat start** (`Trader.reconcile_at_start`, dipanggil sebelum websocket hidup):

| Keadaan di exchange | Yang dilakukan bot |
|---|---|
| Ada SL dan TP | Keduanya dipakai apa adanya sebagai level bot; harga tidak diubah, order tidak disentuh |
| Ada SL, TP hilang | SL dipakai, TP dipasang ulang dari `tp_rr` terhadap level SL |
| TP ada, SL hilang | TP dipakai, SL dipasang dengan jarak dari config, **dihitung dari harga sekarang** dan dipaksa berada di sisi aman (tidak bisa langsung trigger) |
| Tidak ada SL maupun TP | Posisi **tidak** diadopsi, hanya diberi peringatan; SL sintetis tanpa acuan terlalu berbahaya |
| Posisi di simbol di luar universe | Simbol ditambahkan ke universe supaya markPrice dan kline-nya ikut di-stream |

Hal lain yang ikut berubah:

- `POS n/max` dan `max_open_positions` menghitung posisi hasil adopsi. Kalau hasil adopsi sudah memenuhi batas, **tidak ada entry baru** sampai jumlah posisi turun, dan dashboard menampilkan `n adopsi` di baris akun.
- State manajemen diturunkan dari harga saat start: posisi yang sudah lewat `be_trigger_r` ditandai `be`, yang lewat `trail_trigger_r` ditandai `trail`, sehingga SL **tidak pernah dimundurkan**.
- Trailing berbasis ATR tidak langsung jalan untuk posisi adopsi karena ATR saat entry tidak tersimpan di exchange. BE tetap jalan karena hanya butuh harga entry.
- Umur posisi ulang dari nol, jadi `max_hold_minutes` (kalau diaktifkan) mengukur dari waktu adopsi, bukan waktu entry asli.
- Order yang **bukan** milik bot (mis. SL yang Anda pasang manual dengan trigger berbeda) tidak dibatalkan. Hanya satu order SL dan satu TP per simbol yang dipakai.

Mematikan fitur ini: `risk.adopt_existing` = `false` (default `true`). Dengan `false`, perilakunya kembali seperti versi lama: posisi lama dibiarkan di exchange tanpa dikelola bot.

### Apa yang dilanjutkan, apa yang mulai dari nol

Bot **tidak menyimpan state ke disk**. Yang bisa dilanjutkan adalah apa pun yang tersimpan di exchange atau bisa dihitung ulang dari riwayat kline.

| Hal | Setelah restart | Sebab |
|---|---|---|
| Posisi terbuka | **lanjut** | diadopsi dari `/fapi/v3/positionRisk` (lihat di atas) |
| Order SL/TP | **lanjut** | hidup di server Binance |
| Equity dan saldo tersedia | **lanjut** | dibaca ulang dari akun |
| PnL hari ini (`DAY` di dashboard) | **lanjut** | dihitung ulang dari `/fapi/v1/income` sejak 00:00 UTC (= 07:00 WIB) |
| Batas rugi harian | **lanjut** | memakai PnL hari ini di atas, bukan PnL sesi |
| Indikator (EMA, ATR, PAC, tren) | **lanjut, dihitung ulang** | warmup mengunduh sampai 1500 bar terakhir lalu replay dari nol, jadi hasilnya sama dengan sesi sebelumnya |
| Jumlah trade, win/loss, jumlah sinyal | **mulai dari nol** | `DayStats` hanya di memori |
| Panel `RIWAYAT ENTRY` | **lanjut** | disimpan di `logs/<mode>/state.json` (maksimal 200 baris) |
| Cooldown per simbol (`cooldown_bars`) | **lanjut** | disimpan di `logs/<mode>/state.json` sebagai waktu bar, bukan index bar |
| Statistik penolakan (`TOLAK`) | **mulai dari nol** | hanya di memori |
| `peak_price` dan ATR posisi | **hilang untuk posisi hasil adopsi** | tidak tersimpan di exchange |
| Saldo mode paper | **mulai dari nol** | `--paper-balance` mengatur ulang tiap start |

### Zona waktu (WIB)

Default `logging.timezone` = **WIB** (UTC+7, tanpa DST). Yang mengikuti zona ini:

| Bagian | Contoh tampilan |
|---|---|
| jam di kolom `EVENT LOG` dan `RIWAYAT ENTRY` | `18:21:51` WIB |
| `python -m scalpbot signals` | `2026-11-18 01:00 WIB` |
| `python -m scalpbot pnl` | `hari exchange UTC 2026-11-17 (dimulai 07:00 WIB)` |
| nama kolom CSV `signals --csv` | `time_wib` |
| rollover `DayStats` dan reset `daily_loss_limit_pct` | 00:00 WIB |
| label tanggal hari bot | `2026-11-18` (WIB) |

Yang **tidak** ikut dan tidak boleh ikut:

| Bagian | Alasan |
|---|---|
| jendela PnL `/fapi/v1/income` | Binance memakai 00:00 UTC. Menggeser ke WIB membuat `DAY` tidak cocok dengan aplikasi Binance dan berisiko melewatkan rugi antara 00:00–07:00 WIB |
| timestamp candle dari exchange (ms) | selalu UTC, dipakai untuk perhitungan bar |
| `saved_at` di berkas state | metadata internal, sengaja UTC supaya konsisten antar mesin |

Nilai yang diterima: `WIB`, `WITA` (UTC+8), `WIT` (UTC+9), `UTC`/`GMT`, atau nama IANA seperti `Asia/Jakarta`. Salah tulis ditolak saat config dimuat, bukan saat bot jalan.

Kalau ingin kembali ke UTC: `"logging": {"timezone": "UTC"}`.

### Berkas state (`logs/<mode>/state.json`)

Isinya kecil dan hanya dua bagian:

```json
{
  "version": 1,
  "saved_at": "2026-11-17T18:21:51Z",
  "mode": "testnet",
  "symbols": ["DOGEUSDT"],
  "cooldown": {"DOGEUSDT": 1700000000000},
  "entries": [{"time": 1794939711.7, "symbol": "DOGEUSDT", "side": "LONG",
               "price": 0.060006, "qty": 496, "status": "OPEN",
               "detail": "R=251bps", "count": 1}]
}
```

Sifatnya:

- Ditulis **atomik** (file sementara lalu `os.replace`) setiap kali ada entry baru atau penolakan baru, dan sekali lagi saat shutdown. Tidak ada penulisan periodik, jadi tidak ada I/O yang mengganggu jalannya bot.
- Posisi terbuka **tidak** disimpan di sini. Posisi selalu dibaca ulang dari exchange, karena exchange adalah sumber kebenaran.
- Berkas rusak (mis. mesin mati saat menulis) dipindahkan ke `state.json.corrupt` dan bot jalan dengan state kosong, bukan gagal start.
- Versi berbeda diabaikan, sehingga upgrade tidak perlu migrasi manual.
- Satu berkas per mode: `logs/paper/state.json`, `logs/testnet/state.json`, `logs/live/state.json`. Mode paper tidak memakai state mode testnet.
- Mematikan: `logging.persist_state` = `false`. Tanpa state, cooldown dan riwayat kembali ke perilaku lama (mulai dari nol setiap start).
- `python -m scalpbot check` menampilkan isi ringkas berkas state, jadi bisa diperiksa tanpa menjalankan bot.

Hal praktis yang perlu diingat:

- Cooldown **bertahan** setelah restart karena disimpan sebagai waktu bar (bukan index bar yang dihitung ulang dari warmup). Contoh: entry di bar `t`, `cooldown_bars` 3, maka setelah restart entry di bar `t+1` ditolak dengan alasan `cooldown 3 bar (sisa 1)` dan baru lolos di `t+3`.
- Riwayat entry yang dimuat dari state ikut tampil di panel sesaat setelah start, ditandai event `state: riwayat entry sesi sebelumnya dimuat: N baris`.
- `max_open_per_symbol` (default 1) tetap mencegah penumpukan posisi di simbol yang sudah ada hasil adopsi.
- Warmup mengunduh sampai 1500 bar per simbol setiap start, jadi start pertama butuh beberapa detik dan sejumlah weight REST. Ini yang membuat sinyal tidak "melompat" walau state di memori hilang.
- Umur posisi hasil adopsi dihitung dari waktu adopsi, sehingga durasi sebenarnya di exchange tidak terlihat lagi.

---

## 11. Proteksi lewat Algo Order API

Sejak 2025-12-09 Binance memindahkan semua order kondisional (`STOP_MARKET`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET`) ke endpoint algo. Order kondisional yang dikirim ke `/fapi/v1/order` ditolak dengan `-4120 Order type not supported for this endpoint`. Karena itu SL, TP, breakeven, dan trailing sekarang memakai jalur algo.

| Lama (`/fapi/v1/order`) | Sekarang (`/fapi/v1/algoOrder`) |
|---|---|
| `type=STOP_MARKET` + `stopPrice` | `algoType=CONDITIONAL` + `type=STOP_MARKET` + `triggerPrice` |
| `newClientOrderId` | `clientAlgoId` (regex `^[\.A-Z\:/a-z0-9_-]{1,36}$`) |
| respons `orderId` | `algoId` |
| `GET /fapi/v1/openOrders` | `GET /fapi/v1/openAlgoOrders?symbol=` |
| `DELETE /fapi/v1/order` | `DELETE /fapi/v1/algoOrder` atau `DELETE /fapi/v1/algoOpenOrders?symbol=` |

Catatan implementasi:

- Nama parameter lama `stopPrice` **tidak** dipakai. Di endpoint algo, `stopPrice` diterima tanpa error tapi nilainya diabaikan, sehingga order tidak pernah trigger. Bot hanya mengirim `triggerPrice`.
- `algoType=CONDITIONAL` selalu diisi otomatis oleh `client.new_algo_order`, karena tanpa itu Binance membalas `-1102`.
- `open_algo_orders` selalu mengirim `symbol`; tanpa `symbol` weight-nya 40, dengan `symbol` hanya 1 sehingga cocok dengan mode hemat bandwidth.
- Cancel memakai `algoId` (bukan `orderId`). Kode `-2011`/`-2013`/`-4045` (order tidak ada / sudah trigger / posisi sudah tertutup) diabaikan seperti sebelumnya.
- `replace_stop` membaca trigger SL yang sedang aktif lebih dulu, baru membatalkan lalu memasang ulang. Kalau pemasangan baru gagal, trigger lama dipasang kembali supaya posisi tidak pernah telanjang.
- User data stream mengirim event `ALGO_UPDATE` (`X` = `NEW`, `TRIGGERING`, `FINISHED`, `CANCELED`). Saat `FINISHED`, harga `actualPrice`/`ap` dipakai sebagai harga exit lalu rekonsiliasi dijalankan, setara perlakuan `ORDER_TRADE_UPDATE`.
- Order algo milik posisi yang tidak dikenal bot (dibuka manual) hanya dilaporkan di event log, tidak dibatalkan, supaya SL manual pengguna tidak ikut terhapus.
- Batas Binance: maksimal 10 order algo terbuka per simbol; bot hanya memakai 2 (SL + TP) ditambah satu saat replace berlangsung.
- Semua harga trigger dibulatkan ke kelipatan tick sebelum dikirim (`broker._snap`), dan angka float di query bertanda tangan dibulatkan ke 8 desimal tanpa eksponen (`exchange.fmt_num`). Tanpa ini Binance bisa menolak dengan `-1111` karena harga seperti `100.11000000000001`.
- Qty proteksi selalu memakai `executedQty` dari exchange, bukan qty yang diminta. Kalau order market tidak terisi, bot tidak membuka posisi sama sekali.

---

## 12. Pengamanan

- API key hanya dari environment variable, tidak pernah ditulis ke config atau log.
- Mode live butuh kombinasi config `mode: live`, flag `--live`, dan konfirmasi env/terminal.
- Semua order keluar memakai `reduceOnly=true`, jadi tidak bisa membalik arah posisi.
- Kalau SL gagal terpasang setelah entry, posisi langsung ditutup darurat.
- Tiap `reconcile_s` bot memeriksa apakah SL masih ada di exchange; kalau hilang, SL dipasang ulang.
- Kalau ada posisi di exchange yang tidak dikenal bot (dibuka manual), bot memberi peringatan dan tidak mengelolanya.
- `daily_loss_limit_pct` menghentikan entry baru setelah ambang rugi harian. Reset hariannya mengikuti `logging.timezone` (default **WIB**, jadi 00:00 WIB), sedangkan jendela PnL dari Binance tetap 00:00 UTC. Angkanya tidak hanya dari sesi berjalan: sekali per hari bot membaca `/fapi/v1/income` (weight 100) untuk `REALIZED_PNL` + `COMMISSION` + `FUNDING_FEE` sejak 00:00 UTC (= 07:00 WIB), lalu memakai total itu sebagai dasar. Karena itu kolom `DAY` di dashboard bisa sudah terisi `-6.14 (-0.12%)` sejak detik pertama bot hidup: itu PnL akun hari ini dari Binance, bukan PnL sesi. Restart, pindah PC, atau folder `logs/` yang terhapus tidak mengembalikan jatah rugi harian ke nol.
- Semua input config divalidasi tipenya dan batas nilainya sebelum bot start, termasuk nilai `NaN`/`Infinity`.
- Bila `top_volume` tidak menghasilkan kandidat, bot memakai fallback `market.symbols` dan mencatat peringatan, bukan langsung berhenti.
- Log dan event JSONL menyamarkan API key, secret, signature HMAC, dan token alfanumerik 64 karakter atau lebih.
- Snapshot orderbook yang lebih tua dari `orderbook.max_age_s` tidak dipakai untuk keputusan entry.
- Bila cancel-replace SL gagal di tengah jalan, broker mencoba memasang kembali SL di harga sebelumnya.

---

## 13. Struktur

```
scalpbot/
  scalpbot/
    config.py       validasi dan pemuatan config
    indicators.py   EMA, ATR Wilder, Heikin Ashi, fractal, trend
    strategy.py     replika logika Pine (state machine TradeDirection)
    discovery.py    pemilihan simbol otomatis (top_volume)
    risk.py         sizing, SL/TP, BE, trailing, rounding tick/step
    orderbook.py    analisis depth dan filter imbalance
    exchange.py     REST client HMAC, retry, rate limit tracking
    broker.py       eksekusi testnet/live dan simulasi paper
    trader.py       siklus posisi, rekonsiliasi, guard harian
    state.py        penyimpanan cooldown + riwayat entry (JSON atomik per mode)
    dashboard.py    renderer ANSI alternate screen
    events.py       event log di memori, JSONL, rotasi
    netinfo.py      deteksi IP publik
    app.py          orkestrasi asyncio, WebSocket, loop
    __main__.py     CLI: run, check, signals
  tests/            245 unit test
  tools/backtest.py    engine backtest (dipakai bot, bukan simulator terpisah)
  tools/pine_reference.py  transkripsi literal Pine R1.1 untuk uji paritas
  tests/fixtures/btc_5m.json  data nyata untuk uji paritas (tanpa jaringan)
  tools/tune.py        CLI grid parameter + walk-forward
  tools/demo_dashboard.py
  docs/backtest-parameter.md  laporan hasil tuning terakhir
  config.example.json
  requirements.txt
  run.sh, run.bat, run.ps1
```

---

## 14. Backtest dan tuning parameter

Bot ini punya backtest yang memakai **kode strategi dan manajemen risiko yang sama** dengan yang berjalan, jadi angka di tabel di bawah bukan hasil simulator terpisah. Data diambil dari Binance USDⓈ-M (testnet) lewat endpoint kline.

```bash
# 5 simbol default, 45 hari, grid cepat (SL/TP/BE/trailing)
python3 -m tools.tune --days 45 --grid quick --top 15

# lihat sinyal yang lolos filter rezim (dan yang ditolak)
python3 -m scalpbot signals -c config.json --show-blocked

# fokus biaya: efek fee terhadap hasil
python3 -m tools.tune --days 45 --grid cost

# uji walk-forward untuk 3 kombinasi terbaik (65% data latih, 35% data uji)
python3 -m tools.tune --days 45 --grid quick --walk-forward
```

Untuk menguji filter rezim, ubah `strategy.regime_min_slope_bps` (0 = mati, 100 = default) lalu
jalankan `simulate` seperti contoh di atas. Data 312 hari dipakai saat memilih ambang ini.

Data di-cache di `/tmp/bt/data.pkl` supaya percobaan berikutnya tidak mengunduh ulang. Panggilan langsung juga bisa:

```python
from tools.backtest import simulate
from scalpbot.config import Config

cfg = Config()
res = simulate("BTCUSDT", candles, cfg, filters, equity=100.0, warmup=800)
print(res.net_pct, res.profit_factor, res.reasons())
```

### Parameter default sekarang berasal dari backtest

Grid 144 kombinasi pada 5 simbol × 18.000 bar 5m (~62 hari) menunjukkan parameter lama
(SL 1,5×ATR, TP 1,5R, BE 0,5R) **rugi besar**: ekspektasi -0,434 R per trade, profit factor 0,44.
Penyebabnya aritmetika, bukan selera: SL 1,5×ATR hanya ~15 bps sementara fee taker bolak-balik
8 bps + slippage 2 bps, jadi 66% jarak SL sudah habis untuk biaya. TP 1,5R hanya ~23 bps.

Default sekarang (ubah di `config.json` kalau ingin kembali ke nilai lama):

| Kunci | Lama | Baru | Alasan |
|---|---|---|---|
| `sl_atr_mult` | 1.5 | 5.0 | SL ~44 bps, jauh di atas biaya 10 bps |
| `sl_max_bps` | 150 | 250 | Beri ruang saat ATR tinggi |
| `tp_rr` | 1.5 | 4.0 | Upside besar jadi sumber profit, bukan banyak TP kecil |
| `be_trigger_r` | 0.5 | 1.0 | BE tidak terlalu cepat memotong |
| `trail_trigger_r` | 1.0 | 2.5 | Trailing 1R memotong posisi sebelum TP 4R tercapai |
| `min_imbalance` (orderbook) | 0.15 | 0.05 | 0.15 memblokir ~63% entry; lihat bagian 6 |

Hasil pada data uji yang sama:

| Skenario | Trade | Win% | Net% (5 simbol) | Eksp. R | PF |
|---|---|---|---|---|---|
| Default lama | 1438 | 49.8 | -112.6 | -0.434 | 0.44 |
| Default baru (SL/TP saja) | 512 | 49.6 | +38.7 | +0.060 | 1.16 |
| **Default baru + filter rezim** | 891 | 50.4 | **+136.9** | **+0.155** | **1.26** |

Angka filter rezim diukur pada **312 hari** data (Jan–Nov 2026, 90.000 bar per simbol), bukan 62 hari, karena frekuensinya lebih rendah. Pada 62 hari terakhir hasilnya +0,323 R / PF 1,61.

### Baca angka ini dengan benar

- **SL/TP saja belum cukup.** Tanpa filter rezim, +0,060 R punya CI **[-0,068, +0,191]** —
  belum bisa dibedakan dari nol, dan seluruh profitnya hanya dari SHORT.
- **Filter rezim yang membuat beda.** Dengan `regime_min_slope_bps` 100, expR +0,155 dengan
  CI 95% **[+0,050, +0,261]** → signifikan, dan LONG (+0,147) maupun SHORT (+0,159) sama-sama
  positif. Kurva ambangnya monoton (PF 1,03 → 1,56 dari ambang 0 ke 200), bukan bergerigi.
- **Biayanya: frekuensi turun drastis.** 85% sinyal diblokir → ~17 trade/bulan/simbol,
  sekitar 1,7 entry/hari untuk 5 simbol. Dalam sesi testnet 1–2 minggu bisa **nol trade**;
  itu perilaku normal filter ini, bukan bot rusak.
- **2 dari 6 blok 52 hari masih negatif.** Filter belum menyelesaikan masalah pasar sideways
  sepenuhnya, hanya menguranginya.
- **Biaya lebih menentukan daripada parameter.** Pada parameter baru, fee maker 0,02% mengubah
  +38,7% jadi +70,1%. Tapi bot ini **belum punya jalur order limit/maker** — entry dan exit
  memakai MARKET (taker), jadi fee maker butuh pengembangan, bukan sekadar ubah config.
- Net% adalah penjumlahan persen dari 5 akun 100 USDT terpisah, **bukan compounding**. Risiko
  nyata per trade p50 1,48% ekuitas; drawdown per simbol 5,7–18,5%.
- Jangan menaikkan `risk_pct` berdasarkan angka ini. Tetap 1–1,5% sampai testnet 2–4 minggu
  sejalan dengan hasil backtest.

Laporan lengkap (grid, walk-forward, per jendela, per simbol, per arah): [`docs/backtest-parameter.md`](docs/backtest-parameter.md).

---

## 15. Pengujian

```bash
python3 -m unittest discover -s tests -t . -v
```

Cakupan: pemisahan berkas log per mode (paper, testnet, live) termasuk rotasi di folder mode, default discovery `top_volume` dan fallback `market.symbols`, pemilihan simbol otomatis (ranking volume, filter volume, exclude, kelayakan modal, duplikat, data rusak dan non-finite), seeding dan rekursi EMA, Wilder ATR, rantai Heikin Ashi, deteksi fractal regular dan BW, klasifikasi trend, warmup strategi, invarian sinyal (trend harus searah, transisi state harus 0 ke +-1, tidak ada sinyal saat warmup), reset state, pembulatan tick/step, sizing termasuk bump minimum notional dan batas notional, penjepitan SL, BE dengan fee, ratchet trailing, filter orderbook, validasi config, gate live, layout dashboard untuk berbagai ukuran layar, terminal tanpa `SIGWINCH`, pengelompokan kesiapan simbol, meter PAC, tata letak dua panel, siklus posisi paper lengkap (open, BE, trail, stop, target, timeout, guard harian), beberapa posisi per simbol, penandatanganan REST di query string, retry 429/5xx, perintah SUBSCRIBE/UNSUBSCRIBE WebSocket, fallback REST yang membuang candle belum tutup, redaksi secret, rekonsiliasi exit memakai harga fill dari user stream, batas rugi harian yang tahan restart (`/fapi/v1/income`: penjumlahan REALIZED_PNL + COMMISSION, jendela mulai 00:00 UTC, fallback aman saat endpoint gagal, halt saat batas sudah terlampaui sebelum sesi dimulai, dan paper yang tidak memakai endpoint ini), serta jalur Algo Order API: pengiriman `algoType=CONDITIONAL` dan `triggerPrice` (tanpa `stopPrice`), pembuangan parameter kosong, cancel lewat `algoId`, `openAlgoOrders` yang selalu menyertakan `symbol`, rollback SL saat TP gagal, pemulihan trigger lama saat replace gagal, penyaringan `open_stop_ids`, event `ALGO_UPDATE` (`FINISHED` memakai `actualPrice`), posisi manual yang order algonya tidak disentuh, state yang bertahan melewati restart (cooldown berbasis waktu bar yang tetap benar setelah index bar dihitung ulang, penulisan atomik, berkas rusak yang disisihkan, sanitasi nilai tidak valid, riwayat entry yang dipulihkan dan dibatasi 200 baris), serta adopsi posisi saat restart: level SL/TP diambil dari order algo exchange (termasuk pencocokan arah order supaya TP LONG tidak tertukar dengan SL SHORT), pasang ulang TP/SL yang hilang, SL pengganti yang dipaksa berada di sisi aman terhadap harga sekarang, posisi tanpa SL dan TP yang sengaja tidak diadopsi, posisi yang sudah untung tidak boleh memundurkan SL, dan batas `max_open_positions` yang ikut menghitung hasil adopsi.

---

## 16. Batasan yang perlu diketahui

- Posisi hasil adopsi tidak menyimpan ATR saat entry (tidak ada di exchange), jadi trailing berbasis ATR tidak langsung aktif untuk posisi itu; BE tetap jalan. Kalau butuh trailing penuh, tutup lalu buka ulang posisi lewat bot.
- Adopsi hanya mengambil satu order SL dan satu order TP per simbol. Bila ada beberapa order proteksi (mis. sisa order lama), order lain dibiarkan dan tidak dibatalkan.
- Bila `/fapi/v3/positionRisk` gagal dibaca saat start, bot mencatat ERROR dan tetap berjalan tanpa adopsi; dashboard bisa menampilkan `POS 0` padahal exchange punya posisi. Periksa manual di aplikasi Binance.
- Umur posisi hasil adopsi dihitung dari waktu adopsi, sehingga `max_hold_minutes` tidak memakai waktu entry asli.

- **Discovery aktif secara default**: setiap simbol baru memicu satu request kline 1000 bar untuk warmup, dan pemilihan ulang berjalan tiap `discovery_refresh_s` (1800 detik). Dengan `discovery_top_n` 5 bebannya kecil. `market.symbols` bukan daftar yang dipantau, cuma fallback.
- `max_open_per_symbol` bisa diisi lebih dari 1, termasuk di mode paper. Di mode live, rekonsiliasi qty untuk simbol dengan beberapa posisi lokal dilewati karena exchange melaporkan satu posisi agregat; bot tetap mendeteksi kalau posisi itu sudah tutup.
- Kalau SL/TP tereksekusi di exchange dan event `ORDER_TRADE_UPDATE` sempat diterima, bot memakai harga fill asli untuk PnL. Kalau event tidak sempat masuk (misal bot mati), fallback ke harga mark terakhir, jadi PnL di log bisa sedikit berbeda dari mutasi saldo.
- Ada dua "hari" yang berbeda dan sengaja dibiarkan berbeda: **hari exchange** (jendela PnL `/fapi/v1/income`) selalu 00:00 UTC, karena itu ketentuan Binance; **hari bot** (`daily_loss_limit_pct`, rollover `DayStats`, label tanggal) mengikuti `logging.timezone` (default WIB, 00:00 WIB). Kalau keduanya dipaksa sama, angka `DAY` tidak akan cocok dengan aplikasi Binance.
- `DAY` di dashboard = PnL akun hari ini (UTC) menurut `/fapi/v1/income`, bukan PnL sesi. Untuk melihat asalnya per tipe dan per simbol, jalankan `python -m scalpbot pnl -c config.json`. Di mode paper angka ini selalu 0 karena tidak ada exchange.
- Funding fee ikut dihitung ke batas harian (lebih konservatif). Bila ingin persis mengikuti `pnl_net` bot saja, funding bisa diabaikan dengan mengubah `income_today` di `broker.py`.
- Batas rugi harian mengandalkan `/fapi/v1/income`, yang hanya menyimpan 3 bulan terakhir dan maksimal 1000 baris per tipe. Untuk akun dengan ratusan trade per hari keduanya cukup, tetapi bila limit tercapai bot menulis peringatan di log dan angka batasnya bisa kurang tepat. Bila endpoint gagal dibaca, bot tetap jalan dengan PnL sesi ini saja dan menulis peringatan `PnL harian exchange gagal dibaca`. Untuk mode paper endpoint ini tidak dipakai (tidak ada exchange).
- Sinyal mengikuti rumus Pine apa adanya. Tool ini tidak punya filter tren tambahan, jadi di pasar sideways sinyal tetap muncul dan harus disaring manual atau lewat filter orderbook.
- Bot tidak menangani hedge mode (positionSide). Gunakan one-way mode.
- Proteksi memakai Algo Order API sehingga `algoId` tidak muncul di endpoint `/fapi/v1/openOrders` biasa. Kalau memeriksa manual lewat web Binance, SL/TP tampil sebagai order kondisional biasa; lewat REST, gunakan `GET /fapi/v1/openAlgoOrders?symbol=`.
- Order algo tidak ikut dibatalkan oleh `DELETE /fapi/v1/allOpenOrders`. Bot memanggil `cancel_all` dan `cancel_all_algo` terpisah.
- Uji coba live dengan uang nyata belum dilakukan dalam pengerjaan paket ini. Yang sudah diverifikasi: koneksi REST/WebSocket testnet, replay sinyal pada data nyata, seluruh unit test, dan siklus posisi paper.

---

## 17. Troubleshooting

| Gejala | Penyebab umum |
|---|---|
| `config error: ...` | Kunci salah ketik atau nilai di luar batas. Pesan menyebutkan bagian dan kuncinya |
| `API key belum ada` | Env `BINANCE_API_KEY`/`BINANCE_API_SECRET` belum diset di shell yang sama |
| Log lama tercampur antar mode | Bawaan sekarang memisah per folder. Kalau masih tercampur, file lama dari versi sebelumnya ada di `logs/` dan bisa diarsipkan sendiri |
| `-2015 Invalid API-key, IP, or permissions` | IP berubah atau futures trading belum diaktifkan untuk key tersebut |
| `-4164 Order's notional must be no smaller than ...` | Simbol dipilih dengan minimum notional terlalu besar untuk modal, ganti simbol atau naikkan leverage |
| `-4120 Order type not supported for this endpoint` | Order kondisional dikirim ke `/fapi/v1/order`. Versi ini sudah memakai Algo Order API; kalau muncul, pastikan tidak ada skrip lama yang masih memanggil endpoint order biasa |
| `-4045 Maximum number of open algo orders` | Sudah ada 10 order algo terbuka di simbol itu. Batalkan order manual yang menggantung, bot hanya memakai SL + TP |
| `-4131 Order would immediately trigger` | Harga trigger terlalu dekat dengan harga pasar. Bot sudah membulatkan ke tick dan menjaga jarak 2 tick untuk BE; bila masih muncul, naikkan `be_buffer_bps` |
| `PnL harian exchange gagal dibaca` | `/fapi/v1/income` menolak (key tanpa izin futures read, IP berubah, atau jaringan). Batas rugi harian sementara hanya menghitung sesi berjalan |
| Semua sinyal ditolak `imbalance` | Ambang `orderbook.min_imbalance` 0.15 terlalu ketat untuk simbol itu. Jalankan `./run.sh bookscan` lalu turunkan ambang atau set 0.0 |
| Entry ditolak `spread ... > 5.0bps` | Book sedang lebar (berita, jam sepi). Naikkan `orderbook.max_spread_bps` bila ingin tetap entry |
| Entry ditolak `snapshot orderbook terlalu lama` | `max_age_s` 15 detik terlampaui; aktifkan `orderbook.periodic_snapshot_s` |
| `-4046 No need to change margin type` | Normal, diabaikan otomatis |
| WS `DOWN` terus | Jaringan memblokir `wss://fstream.binancefuture.com`; coba `net.proxy`, cek event `ack stream` di level DEBUG |
| Dashboard berisi kode escape di Windows | Terminal tidak mendukung VT; pakai Windows Terminal atau jalankan dengan `--no-dashboard` |
| `config error` untuk `NaN`/`Infinity` | JSON tidak boleh memuat konstanta non-finite, pakai angka biasa |
| `-4131` muncul setelah restart | Dulu SL pengganti bisa dihitung dari entry sehingga jatuh di sisi harga yang salah. Sekarang SL pengganti dihitung dari harga sekarang; kalau masih muncul, periksa log `adopt` untuk simbol tersebut |
| `POS 0/3` padahal exchange punya posisi | Adopsi dimatikan (`risk.adopt_existing=false`), atau `/fapi/v3/positionRisk` gagal dibaca saat start. Lihat event `adopt` di log |
| Posisi lama tidak di-trail | Posisi hasil adopsi tidak punya ATR saat entry, jadi trailing ATR belum aktif; BE tetap jalan |
| `discovery tidak menghasilkan simbol` | Semua kandidat tersaring batas volume atau minimum notional lebih besar dari modal. Bot memakai fallback `market.symbols`; turunkan `discovery_quote_volume_min` atau matikan `discovery_require_affordable` bila ingin tetap otomatis |
| Sinyal tidak muncul setelah simbol baru masuk | Simbol baru masih warmup 600 bar lebih; cek kolom `SINYAL` dan event `warmup` |
| Tidak ada sinyal berjam-jam | Normal untuk 5m dengan EMA 600; lihat `./run.sh signals` untuk memastikan strategi memang menghasilkan sinyal pada data terkini |

---

## 18. Tanggung jawab

Bot ini mengirim order nyata bila dijalankan dengan `--live`. Semua keputusan trading dan konsekuensi finansial ada pada pemilik akun. Uji di testnet dan paper sampai yakin, mulai dari leverage kecil, dan jangan jalankan dengan modal yang tidak siap hilang.

---

## 19. Sumber dokumentasi

Diambil pada 2026-11-16:

- [Binance USD-M Futures General Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info): parameter GET wajib di query string, POST/PUT/DELETE boleh di query string atau body `application/x-www-form-urlencoded`, penanganan HTTP 429/5xx, dan URL testnet baru. Bot memilih query string untuk semua request bertanda tangan.
- [Binance User Data Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Start-User-Data-Stream): `POST`/`PUT`/`DELETE /fapi/v1/listenKey` hanya butuh header `X-MBX-APIKEY`, stream kedaluwarsa setelah 60 menit.
- [Binance New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api): aturan `newClientOrderId`, `reduceOnly`, dan `newOrderRespType` untuk order market entry.
- [Binance New Algo Order / Query / Cancel](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order): `algoType=CONDITIONAL`, `triggerPrice`, `clientAlgoId`, `workingType`, `closePosition`, respons `algoId`/`algoStatus`/`actualOrderId`.
- [Binance Get Income History](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Get-Income-History): `GET /fapi/v1/income` dengan `incomeType` `REALIZED_PNL`/`COMMISSION`, rentang `startTime`/`endTime`, `limit` maksimal 1000, weight 100, hanya 3 bulan terakhir. Dipakai sebagai dasar batas rugi harian yang tahan restart.
- [Binance Current All Algo Open Orders](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Current-All-Algo-Open-Orders): parameter `symbol`/`algoType`, weight 1 dengan `symbol` dan 40 tanpa `symbol`.
- [TradingView Pine Script v4 Reference, `ema()`](https://www.tradingview.com/pine-script-reference/v4/#fun_ema): implementasi referensi memakai seed SMA, sesuai `indicators.EMA` di bot ini.
- [PyPI JSON API](https://pypi.org/pypi/requests/json) dan [websocket-client](https://pypi.org/pypi/websocket-client/json): versi `requests` 2.34.2 dan `websocket-client` 1.9.2.

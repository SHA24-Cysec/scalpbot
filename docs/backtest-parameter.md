# Laporan backtest dan tuning parameter

Data: Binance USDⓈ-M **5 menit**, 5 simbol (BTC, ETH, BCH, XRP, DOGE). Dua rentang dipakai:

- **312 hari** (Jan–Nov 2026, 90.000 bar/simbol) untuk memilih dan menguji filter rezim.
- **62 hari** (Sep–Nov 2026) untuk uji awal parameter SL/TP, karena itu data terbaru.

Fee taker 0,04% per sisi, slippage 1 bps per sisi.

Simulasi memakai kode yang sama dengan bot: `strategy.ScalpPullbackStrategy` dan
`risk.plan_stops / trailing_stop / breakeven_price`. Aturan konservatif: bila SL dan TP
tersentuh pada bar yang sama, SL dianggap lebih dulu kena.

> **Ringkasnya:** parameter SL/TP mengubah ekspektasi dari -0,43 R menjadi +0,06 R, tapi angka itu
> masih di dalam noise. Yang membuat hasilnya berbeda adalah **filter rezim tren**: +0,155 R
> dengan CI 95% [+0,050, +0,261] — signifikan pada 312 hari data, dan profitnya tidak lagi
> hanya dari satu arah.

## 1. Parameter lama vs baru

| Metrik | Lama (SL 1,5×ATR, TP 1,5R, BE 0,5R, trail 1,0R) | Baru (SL 5×ATR, TP 4R, BE 1R, trail 2,5R) |
|---|---|---|
| Jumlah trade | 1438 | 512 |
| Win rate | 49,8% | 49,6% |
| Net (5 akun @100 USDT, bukan compounding) | -112,6% | +38,7% |
| Ekspektasi per trade | **-0,434 R** | **+0,060 R** |
| Profit factor | 0,44 | 1,16 |
| Drawdown per simbol | 5,7–18,5% | 5,7–18,5% |

## 2. Kenapa parameter lama rugi (aritmetika, bukan opini)

- ATR 5m di 5 simbol ini: median **10,2 bps** dari harga.
- SL 1,5×ATR → hanya **~15 bps** dari entry.
- Fee taker bolak-balik 8 bps + slippage 2 bps = **10 bps** ≈ **66% jarak SL**.
- TP 1,5R → hanya **~23 bps**.
- Hasil: untuk menang, harga harus bergerak 23 bps sebelum menyentuh 15 bps ke arah salah,
  sementara 10 bps sudah dipotong biaya. Win rate 49,8% dengan rasio 1,5:1 seharusnya profit,
  tapi biaya memakan seluruhnya.

Edge sinyal 5m sendiri hanya **+4 bps pada horizon 12 bar (1 jam)**. Artinya sinyalnya tipis;
yang paling menentukan hidup-mati adalah jarak SL dan frekuensi trade, bukan filter tambahan.

## 3. Grid yang diuji

144 kombinasi `sl_atr_mult` (1,5–10) × `tp_rr` (1,5–5) × `be_trigger_r` (0,5–2) × setting trailing.
Semua kombinasi dengan SL sempit tetap negatif. Yang mendekati nol atau positif semuanya punya
SL ≥ 4×ATR dan TP ≥ 3R.

| Skenario | Trade | Win% | Net% | Eksp. R | PF |
|---|---|---|---|---|---|
| Lama (SL 1,5 / TP 1,5 / BE 0,5) | 1438 | 49,8 | -112,6 | -0,434 | 0,44 |
| SL 3 / TP 2,5 / BE 1 | 990 | 48,6 | -190,3 | -0,128 | 0,81 |
| SL 4 / TP 3 / BE 1,5 | 790 | 49,1 | -99,1 | -0,036 | 0,93 |
| SL 6 / TP 3 / BE 1,5 | 580 | 49,3 | -23,4 | +0,020 | 1,03 |
| **Baru: SL 5 / TP 4 / BE 1 / trail 2,5R** | **512** | **49,6** | **+38,7** | **+0,060** | **1,16** |
| Baru + fee maker 0,02% | 499 | 49,7 | +70,1 | +0,137 | 1,30 |
| Lama + fee maker 0,02% | 1441 | 51,8 | -72,2 | -0,236 | 0,62 |

Temuan pendukung: trailing 1R memotong posisi sebelum TP 4R tercapai. Menunda trailing ke 2,5R
menaikkan hasil pada grid yang sama dari +14,7% menjadi +47%. **Biaya lebih menentukan daripada
parameter:** pada parameter lama, menurunkan fee dari 0,04% ke 0,02% hanya mengubah -112,6%
menjadi -72,2% (tetap rugi). Pada parameter baru, perubahan fee yang sama mengubah +38,7% menjadi
+70,1%.

## 4. Apakah ini edge nyata? (jawabannya: belum terbukti)

| Uji | Hasil | Kesimpulan |
|---|---|---|
| Bootstrap 95% CI, fee taker 0,04% | expR +0,060, CI **[-0,068, +0,191]** | tidak bisa dibedakan dari nol |
| Bootstrap 95% CI, fee maker 0,02% | expR +0,137, CI **[+0,005, +0,272]** | signifikan secara teknis, tapi baru 499 trade |
| Walk-forward 65%/35% | IS -0,014 → OOS **+0,244** | arah bagus, tapi OOS > IS itu mencurigakan sendiri |
| Per jendela 8,7 hari (7 jendela) | +20,3 / -21,2 / +16,5 / -10,8 / -11,4 / +5,7 / +32,9 | **3 dari 7 jendela negatif** |
| Arah LONG vs SHORT | LONG -0,083 R, SHORT +0,184 R | **edge tidak simetris**, hanya datang dari SHORT |
| Leave-one-out (buang 1 simbol) | PF 1,10–1,21 di semua kasus | tidak bergantung pada satu simbol |
| Harga sampel | rata-rata turun **-23,7%** (semua simbol turun) | pasar sedang downtrend kuat |

Poin yang paling penting: **seluruh profit berasal dari SHORT, dan periode sampel adalah
downtrend -23,7%.** Sinyal LONG kalah di semua kecuali satu jendela. Jadi hasil positif ini
belum membuktikan strategi punya edge dua arah; yang lebih mungkin terjadi adalah strategi
ikut arus turun.

Kekuatan statistik: sd R = 1,51. Untuk membuktikan expR +0,06 lebih besar dari nol dengan
power 80% dibutuhkan sekitar **5000 trade**. Sampel ini 512 trade. Jangan perlakukan angka
+38,7% sebagai proyeksi.

## 5. Per simbol (config baru, fee taker 0,04%)

| Simbol | Trade | Win% | Eksp. R | Net% | DD% | Margin p50/p90 (% equity, leverage 10×) |
|---|---|---|---|---|---|---|
| BTCUSDT | 103 | 50,5 | -0,004 | -4,5 | 5,7 | 35% / 73% |
| ETHUSDT | 118 | 47,5 | -0,035 | -4,4 | 15,2 | 24% / 48% |
| BCHUSDT | 105 | 50,5 | +0,161 | +17,2 | 14,1 | 15% / 22% |
| XRPUSDT | 99 | 50,5 | +0,163 | +15,8 | 8,6 | 16% / 33% |
| DOGEUSDT | 87 | 49,4 | +0,023 | -4,9 | 18,5 | 15% / 29% |

Risiko nyata per trade setelah sizing: **p50 1,48% ekuitas** (target 1,5%), maksimum 45%
dari `max_notional_frac_equity`. Notional/ekuitas median 3,4× karena SL lebar butuh qty besar
untuk mencapai risiko 1,5%.

## 6. Filter rezim tren

### Temuan yang memicunya

Pada data 62 hari, seluruh profit datang dari SHORT. Saat diuji per blok, penyebabnya bukan
arah sinyal, tapi **pasar sideways**:

| Blok ~52 hari | Tren pasar | expR tanpa filter | LONG | SHORT |
|---|---|---|---|---|
| Jan–Mar | -19,4% | +0,128 | +7 | +60 |
| Mar–Apr | **+6,0%** | **-0,063** | -17 | -22 |
| Apr–Jun | -14,9% | +0,098 | -22 | +55 |
| Jun–Agu | **-1,4%** | **-0,002** | +6 | -24 |
| Agu–Sep | **+28,6%** | -0,022 | **+86** | -46 |
| Sep–Nov | -22,5% | +0,126 | -20 | +72 |

Dua blok paling rugi adalah dua blok dengan pergerakan pasar paling datar. Sebaliknya pada
blok uptrend +28,6%, strategi justru untung besar dari LONG (+86) dan rugi dari SHORT (-46).

### Yang diuji

Strategi ini sudah menghitung `slow_ema` (600) tetapi hasilnya tidak pernah dipakai untuk
keputusan. Filter rezim memakainya: sinyal hanya diterima kalau `slow_ema` bergerak minimal
`regime_min_slope_bps` ke arah yang sama dalam `regime_slope_bars` bar.

```
strategy.regime_slope_bars:   200
strategy.regime_min_slope_bps: 100.0    # 0 = filter mati (perilaku lama)
```

### Kurva ambang (312 hari, taker 0,04%)

| min_slope | Trade | expR | PF | Blok negatif | Trade/bulan/simbol |
|---|---|---|---|---|---|
| 0 (mati) | 2739 | +0,002 | 1,03 | 3/6 | 52,5 |
| 20 | 1792 | +0,060 | 1,12 | 2/6 | 34,5 |
| 40 | 1500 | +0,053 | 1,11 | 2/6 | 28,8 |
| 60 | 1272 | +0,094 | 1,15 | 1/6 | 24,5 |
| 80 | 1073 | +0,115 | 1,19 | 2/6 | 20,6 |
| **100** | **890** | **+0,155** | **1,26** | 2/6 | 17,1 |
| 130 | 647 | +0,165 | 1,30 | 2/6 | 12,4 |
| 160 | 477 | +0,206 | 1,39 | 2/6 | 9,2 |
| 200 | 326 | +0,289 | 1,56 | 1/6 | 6,3 |

Kurvanya **monoton**: makin ketat filternya, makin baik per trade (PF 1,03 → 1,56). Kurva
yang bergerigi naik-turun akan menandakan kebetulan; yang monoton begitu menandakan efek nyata.

### Apakah ini lebih baik daripada sekadar mematikan LONG?

Ini uji terpenting, dan jawabannya **ya**:

| Skenario | Trade | expR | PF |
|---|---|---|---|
| Tanpa filter, dua arah | 2739 | +0,002 | 1,03 |
| **Hanya SHORT** (LONG dimatikan total) | 1479 | +0,070 | 1,14 |
| Hanya LONG | 1260 | -0,077 | 0,91 |
| Filter slope ≥ 30, dua arah | 1636 | +0,065 | 1,12 |
| **Filter slope ≥ 100, dua arah** | **890** | **+0,155** | **1,26** |

Filter 100 mengalahkan "buang LONG", jadi nilainya bukan cuma menyaring arah — ia juga
menyaring SHORT saat pasar tidak sedang turun.

### Signifikansi dan ketahanan

| Uji | Hasil |
|---|---|
| Bootstrap CI 95%, min_slope 100 | **expR +0,155, CI [+0,050, +0,261]** → signifikan |
| Bootstrap CI 95%, min_slope 130 | +0,165, CI [+0,045, +0,294] → signifikan |
| Bootstrap CI 95%, min_slope 160 | +0,206, CI [+0,058, +0,359] → signifikan |
| `regime_slope_bars` 100 / 200 / 400 | expR +0,090 / +0,065 / +0,060 (min_slope 30) → stabil |
| Walk-forward 60%/40%, min_slope 100 | IS +0,105 R → OOS +0,262 R |
| Per simbol (min_slope 100) | kelima simbol positif (PF 1,07–1,45) |
| LONG vs SHORT | LONG +0,147, SHORT +0,159 → **dua arah seimbang** |

Berbeda dari versi tanpa filter, di sini LONG dan SHORT sama-sama positif. Itu tanda filter
benar-benar menyeleksi rezim, bukan cuma memihak arah yang sedang menang.

### Biaya perdagangan (risiko yang harus dibaca)

Filter ini memblokir **85% sinyal** (5109 dari 6000). Frekuensi turun menjadi ~17 trade per
bulan per simbol, artinya ~1,7 trade/hari untuk 5 simbol. Konsekuensinya:

- Butuh waktu jauh lebih lama untuk mengumpulkan sampel. 890 trade = ~10 bulan data.
- Dalam periode pendek (1–2 minggu testnet), bisa saja **nol trade**. Itu normal, bukan kerusakan.
- Bila `min_slope` dinaikkan ke 160–200, frekuensi menjadi 6–9 trade/bulan/simbol.

### Hasil akhir pada dua rentang data

| Rentang | Trade | Win% | Net% | expR | CI 95% | PF | LONG | SHORT |
|---|---|---|---|---|---|---|---|---|
| 312 hari | 891 | 50,4 | +136,9 | +0,155 | [+0,050, +0,261] | 1,26 | +0,147 | +0,159 |
| 62 hari | 176 | 55,7 | +57,3 | +0,323 | [+0,089, +0,563] | 1,61 | +0,068 | +0,445 |

## 7. Filter tren kuat vs sideways

### Apakah pasar 5m ini punya tren?

Diukur dengan efisiensi pergerakan per 200 bar: `|harga akhir - awal| / jumlah |perubahan per bar|`. Nilai 1 = tren mulus sempurna, mendekati 0 = choppy.

| Simbol | p10 | **p50** | p90 |
|---|---|---|---|
| BTCUSDT | 0,011 | 0,067 | 0,176 |
| ETHUSDT | 0,012 | 0,061 | 0,170 |
| BCHUSDT | 0,011 | 0,060 | 0,159 |
| XRPUSDT | 0,011 | 0,064 | 0,150 |
| DOGEUSDT | 0,011 | 0,057 | 0,156 |

**Median 0,06 artinya pasar 5m di 5 simbol ini choppy hampir sepanjang waktu.** Ini bukan pasar yang sedang tren; ini pasar yang bergerak bolak-balik dalam rentang sempit.

### Apakah filter rezim sekarang menolak sideways?

Sebagian, tapi tidak tajam. Dari 7.406 sinyal mentah:

| | n | efisiensi p50 | edge 12 bar |
|---|---|---|---|
| Lolos filter rezim | 2.153 | 0,094 | +0,26 bps |
| Ditolak | 5.253 | 0,069 | +0,21 bps |

Sinyal yang lolos memang lebih mulus, tapi 0,094 masih tergolong choppy. Filter slope EMA 200 itu mengukur **arah** gerakan, bukan **kualitas** gerakan.

### Kalau ditambah gerbang efisiensi

Gerbang `efisiensi >= X` dipasang setelah filter rezim:

| Efisiensi min | Trade | expR | Net% | PF |
|---|---|---|---|---|
| 0 (sekarang) | 889 | +0,155 | +137,5 | 1,26 |
| 0,05 | 768 | +0,160 | +143,0 | 1,31 |
| 0,06 | 728 | +0,167 | +138,4 | 1,31 |
| **0,08** | **639** | **+0,208** | **+162,9** | **1,43** |
| 0,10 | 530 | +0,191 | +120,0 | 1,39 |
| 0,12 | 416 | +0,163 | +75,6 | 1,32 |

Puncaknya di 0,08 lalu turun. Kurva naik-turun menandakan sebagian hasil itu kebetulan, jadi jangan menganggap 0,08 sebagai angka keramat.

Uji ketahanan pada 0,08: 4 dari 5 simbol membaik (BTC +0,042, BCH +0,050, XRP +0,166, DOGE +0,006, ETH -0,007), dan kedua paruh waktu membaik (+0,033 dan +0,044). CI 95%: [+0,076, +0,332] berbanding [+0,051, +0,260] sekarang. Keduanya signifikan, tapi irisan CI-nya masih lebar, jadi peningkatannya belum bisa disebut pasti.

**Belum diterapkan ke bot.** Alasannya: efisiensi pergerakan bukan bagian dari indikator Pine, dan menambahkannya membuat sinyal makin jauh dari chart. Butuh keputusan Anda.

## 8. Yang tidak dimodelkan

- Tidak ada order yang ditolak exchange, requote, atau kegagalan Algo Order API.
- Tidak ada funding fee (posisi di sini ditutup dalam hitungan jam, dampaknya kecil tapi tidak nol).
- Slippage tetap 1 bps; pada berita atau saat spread melebar, angka nyata bisa 3–5 bps.
- Fee maker 0,02% **belum bisa dipakai bot ini**: entry dan exit saat ini memakai MARKET
  (taker). Untuk mendapat maker, bot perlu jalur order limit + logika "batalkan kalau tidak
  terisi dalam N detik", dan itu belum ada.
- Hanya 5m yang diuji cukup dalam. 15m/30m/1h punya sinyal terlalu sedikit (66–453) untuk diuji.

## 9. Cara mengulang

```bash
python3 -m tools.tune --days 45 --grid quick --walk-forward
python3 -m tools.tune --days 45 --grid cost
```

Data di-cache di `/tmp/bt/data.pkl`. Hapus file itu untuk mengunduh ulang data terbaru.

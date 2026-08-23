# Конспект по API майданчиків

Усе, що потрібно для адаптерів. Реалізація — у `spreadbot/adapters/`.

## Polymarket

* **Gamma (метадані):** `GET https://gamma-api.polymarket.com/markets?closed=false&limit=&offset=&order=volumeNum`
* **CLOB (стакани):** `GET https://clob.polymarket.com/book?token_id=<id>`
* **Історія:** `GET https://clob.polymarket.com/prices-history?market=<token>&interval=max`
* **WebSocket:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`, канал `book`

Пастки:

* `outcomes`, `outcomePrices`, `clobTokenIds` приходять **рядками з JSON усередині** — треба `json.loads`;
* ціна токена = ймовірність (0..1), розмір у стакані = кількість контрактів по $1;
* правило резолву — в полі `description`, його треба зберігати;
* торгова комісія на крипто-ринках (наш кейс): **taker fee = shares ·
  0,07 · P · (1−P)**, максимум $1,75 на 100 шейрів (1,75%) при P=0,5, від
  кількості куплених шейрів, не від вкладених доларів; **мейкери (лімітні
  ордери) комісії не платять ніколи**. Роллаут: крипто 15-хв контракти з
  5 січня 2026, усі крипто-таймфрейми з 6 березня 2026, фінальний Fee
  Schedule V2 (крипто=0,07, спорт=0,03, фінанси/політика/tech/mentions=
  0,04, економіка/культура/погода/інше=0,05, геополітика/world events —
  без комісії) з 30 березня 2026 — тобто станом на сьогодні вже діє
  повністю. У боті — `strategy/costs.py`, `PredictionFees.parabolic_taker_fee`,
  коефіцієнт редагується без зміни коду через `scan.polymarket_taker_fee_coeff`
  (config.yaml).

  **Джерело й рівень довіри:** пряма мережа до `docs.polymarket.com`
  у середовищі збірки заблокована (проксі повертає 403 на CONNECT), але
  інструмент `WebSearch` йде НЕ через цей проксі й спрацював — формула
  й коефіцієнт вище перехресно підтверджені кількома незалежними
  агрегаторами (startpolymarket.com, crypticorn.com, predictionhunt.com,
  oddsshopper.com, Polymarket Help Center за назвою статті у видачі),
  а не одним джерелом, як було раніше (стара оцінка 0,072/«з 1 квітня»
  походила з одного числового прикладу в сторонньому Discord-гайді й
  виявилась близькою, але неточною — виправлено на 0,07/фактичні дати
  вище). Офіційна сторінка Polymarket так і не прочитана НАПРЯМУ — це
  досі вторинні джерела, просто кілька незалежних і взаємно узгоджених;
* читання не потребує ключа; торгівля — підпис L2-заявки (EIP-712).

**Статус звірки з живими даними:** адаптер (`adapters/prediction/polymarket.py`)
написаний за пам'яттю про форму Gamma/CLOB API й перевірений лише проти
власноруч згенерованих фікстур (`tools/make_fixtures.py`) — тобто це
замкнене коло, не незалежна перевірка (сюди WebSearch не допомагає:
воно дає текстові описи API, а не факт, що поля JSON-відповіді
розпізнаються нашим кодом так, як задумано). Поля `outcomes`/
`clobTokenIds`/`outcomePrices`, назви ендпоінтів, пагінація й фільтр
`active/closed` жодного разу не звірялись з реальною відповіддю сервера.
Перший чесний тест — `python3 -m spreadbot markets` чи `python3 -m
spreadbot chain --asset ETH` на машині з мережею; якщо поля не
збігаються, `_to_market` поверне `None` (ринок мовчки пропущений) або
впаде з винятком — дивіться `-v`/лог, а не тільки на кількість
знайдених ринків.

## Kalshi

* `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=`
* `GET .../markets/{ticker}/orderbook`

Пастки:

* ціни в центах (1..99);
* стакан віддає **тільки біди** обох сторін: аск YES = 100 − бід NO;
* комісія `ceil(0,07 · контракти · P · (1 − P))`, максимальна біля P=0,5;
* приватні ендпоінти — RSA-підпис (`KALSHI-ACCESS-KEY`, `-SIGNATURE`, `-TIMESTAMP`).

## Deribit

* `GET /api/v2/public/get_instruments?currency=ETH&kind=option&expired=false`
* `GET /api/v2/public/get_book_summary_by_currency?currency=ETH&kind=option`
* `GET /api/v2/public/get_order_book?instrument_name=ETH-25JUN27-2600-C&depth=10`
* `GET /api/v2/public/get_index_price?index_name=eth_usd`
* Тестова мережа: `test.deribit.com`

Пастки:

* **премія котирується в ETH** → USD = `price × underlying_price`;
* `underlying_price` у відповіді — це **форвард** експірації, саме він іде в Black-76;
* `mark_iv` — у відсотках (65,3 = 0,653);
* формат символу `ETH-25JUN27-2600-C`, експірація о 08:00 UTC;
* мінімальний лот 1 ETH; комісія 0,03% від нотіоналу зі стелею 12,5% премії,
  делівері 0,015%.

## Bybit v5

* `GET /v5/market/instruments-info?category=option&baseCoin=ETH`
* `GET /v5/market/tickers?category=option&baseCoin=ETH`
* `GET /v5/market/orderbook?category=option&symbol=ETH-27JUN26-3000-C`

Пастки:

* премія одразу в USDC (лінійні опціони) — конвертувати не треба;
* `tickers` віддає тільки перший рівень книги (`bid1Price`/`ask1Size`);
* мінімальний лот 0,1 ETH — критично для малого капіталу;
* є `underlyingPrice` (форвард) і `indexPrice` (спот) окремо.

## Джерела ціни для звірки резолву

| Джерело | Навіщо |
|---|---|
| Deribit index (`eth_usd`) | за ним розраховуються опціони |
| Binance `klines` 1m | за ним часто резолвляться touch-ринки |
| Coinbase / Chainlink / Pyth | зустрічаються в правилах резолву on-chain ринків |

## Ліміти

Практичні значення в `util/http.py`: Polymarket ~4 запити/с, Deribit ~8/с,
беквоф 2/4/8/16 с на 429 і 5xx. Сирі відповіді пишуться в `cache_dir` —
з них потім робляться фікстури і бектест.

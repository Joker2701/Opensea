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
* торгової комісії немає, але є газ/релеєр на Polygon;
* читання не потребує ключа; торгівля — підпис L2-заявки (EIP-712).

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

import requests
import json
import time

# Змінні для зберігання API-ключа та URL API Opensea
api_key = 'YOUR_API_KEY'
api_url = 'https://api.opensea.io/api/v1/'

# Функція для отримання списку всіх колекцій на Opensea
def get_collections():
    url = api_url + 'collections'
    headers = {'Accept': 'application/json'}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return json.loads(response.content.decode('utf-8'))
    else:
        return None

# Функція для отримання списку всіх NFT-лотів зі списку колекцій
def get_nft_lots(collection):
    url = api_url + 'events'
    params = {'only_opensea': 'true', 'offset': '0', 'limit': '20', 'collection_slug': collection}
    headers = {'Accept': 'application/json'}
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        return json.loads(response.content.decode('utf-8'))
    else:
        return None

# Функція для отримання мінімальної ціни для заданого NFT-лота
def get_min_price(nft_lot):
    if 'sell_orders' not in nft_lot:
        return None

    sell_orders = nft_lot['sell_orders']
    if len(sell_orders) == 0:
        return None

    min_price = None
    for order in sell_orders:
        if 'current_price' in order:
            price = int(order['current_price'])
            if min_price is None or price < min_price:
                min_price = price

    return min_price

# Головна функція для перевірки кожного NFT-лота на мінімальну ціну
def check_nft_lots():
    collections = get_collections()

    for collection in collections:
        print('Пошук лотів в колекції', collection['name'], '...')

        nft_lots = get_nft_lots(collection['slug'])
        if nft_lots is None:
            continue

        for nft_lot in nft_lots['asset_events']:
            min_price = get_min_price(nft_lot)
            if min_price is None:
                continue

            if 'top_bid' in nft_lot and nft_lot['top_bid']['amount'] > min_price:
                print('Увага! Лот', nft_lot['asset']['name'], 'має пропозицію', nft_lot['top_bid']['formatted_amount'], 'що вище мінімальної ціни в', min_price, 'для цього лота.')
            else:
                print('Лот', nft_lot['asset']['name'], 'має мінімальну ціну в', min_price, 'і пропозицію не вище.')
        
        time.sleep(1)

if __name__ == '__main__':
    while True:
        check_nft_lots()

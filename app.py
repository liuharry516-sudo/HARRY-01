from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import json
import time
import pytz
from datetime import datetime, timedelta
from cachetools import TTLCache, cached
import threading
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Cache: 60s for real-time, 300s for semi-static
rt_cache = TTLCache(maxsize=200, ttl=60)
static_cache = TTLCache(maxsize=100, ttl=300)
cache_lock = threading.Lock()

TW_TZ = pytz.timezone('Asia/Taipei')

# ── TWSE helpers ─────────────────────────────────────────────────────────────

def twse_get(url, params=None):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; StockApp/1.0)'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8)
        return r.json()
    except Exception as e:
        logger.error(f"TWSE error {url}: {e}")
        return None

def get_taiex():
    key = 'taiex'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
    data = twse_get(url, params={'ex_ch': 'tse_t00.tw', 'json': 1, 'delay': 0})
    result = {}
    if data and 'msgArray' in data and data['msgArray']:
        m = data['msgArray'][0]
        result = {
            'name': '加權指數',
            'price': float(m.get('z', m.get('y', 0)) or 0),
            'change': float(m.get('z', 0) or 0) - float(m.get('y', 0) or 0),
            'change_pct': 0,
            'volume': int(m.get('v', 0) or 0),
            'open': float(m.get('o', 0) or 0),
            'high': float(m.get('h', 0) or 0),
            'low': float(m.get('l', 0) or 0),
            'prev_close': float(m.get('y', 0) or 0),
        }
        if result['prev_close']:
            result['change_pct'] = round(result['change'] / result['prev_close'] * 100, 2)
    # fallback: TWSE open data
    if not result:
        url2 = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=MS&response=json'
        data2 = twse_get(url2)
        if data2 and 'data' in data2:
            for row in data2['data']:
                if '加權' in str(row):
                    try:
                        result = {
                            'name': '加權指數',
                            'price': float(str(row[1]).replace(',', '')),
                            'change': float(str(row[2]).replace(',', '').replace('+', '')),
                            'change_pct': float(str(row[3]).replace('%', '')),
                        }
                    except:
                        pass
    with cache_lock:
        rt_cache[key] = result
    return result

def get_twse_stocks(stocks_list=None):
    """Get multiple TW stocks from TWSE mis API"""
    if not stocks_list:
        stocks_list = ['2330', '2317', '2454', '2382', '3711', '2308', '2303', '1301', '2881', '2882',
                       '2886', '2891', '2892', '5880', '2002', '1303', '1326', '2412', '3008', '4938']
    key = 'twse_stocks_' + '_'.join(stocks_list[:5])
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    ex_ch = '|'.join([f'tse_{s}.tw' for s in stocks_list])
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
    data = twse_get(url, params={'ex_ch': ex_ch, 'json': 1, 'delay': 0})
    results = []
    if data and 'msgArray' in data:
        for m in data['msgArray']:
            if not m.get('z') or m['z'] == '-':
                continue
            price = float(m.get('z', 0) or 0)
            prev = float(m.get('y', 0) or 0)
            chg = price - prev
            chg_pct = round(chg / prev * 100, 2) if prev else 0
            results.append({
                'symbol': m.get('c', ''),
                'name': m.get('n', ''),
                'price': price,
                'change': round(chg, 2),
                'change_pct': chg_pct,
                'volume': int(m.get('v', 0) or 0),
                'high': float(m.get('h', 0) or 0),
                'low': float(m.get('l', 0) or 0),
                'open': float(m.get('o', 0) or 0),
                'prev_close': prev,
                'market': 'TW',
            })
    with cache_lock:
        rt_cache[key] = results
    return results

def get_twse_top_movers():
    key = 'top_movers'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    # TWSE 漲跌幅排行
    url = 'https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json&selectType=ALL'
    data = twse_get(url)
    gainers, losers = [], []
    if data and 'data' in data:
        for row in data['data'][:50]:
            try:
                gainers.append({'symbol': row[0], 'name': row[1], 'pe': row[4], 'pb': row[5]})
            except:
                pass
    result = {'gainers': gainers[:10], 'losers': losers[:10]}
    with cache_lock:
        rt_cache[key] = result
    return result

def get_twse_market_summary():
    key = 'market_summary'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    url = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=MS&response=json'
    data = twse_get(url)
    result = {}
    if data:
        result['date'] = data.get('date', '')
        result['stat'] = data.get('stat', '')
    with cache_lock:
        static_cache[key] = result
    return result

def get_stock_history_twse(symbol, days=30):
    key = f'history_{symbol}_{days}'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    end = datetime.now(TW_TZ)
    start = end - timedelta(days=days + 10)
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?stockNo={symbol}&response=json'
    data = twse_get(url)
    rows = []
    if data and 'data' in data:
        for row in data['data']:
            try:
                date_str = row[0].replace('/', '-')
                # convert ROC year
                parts = date_str.split('-')
                year = int(parts[0]) + 1911
                date = f"{year}-{parts[1]}-{parts[2]}"
                rows.append({
                    'date': date,
                    'open': float(str(row[3]).replace(',', '')),
                    'high': float(str(row[4]).replace(',', '')),
                    'low': float(str(row[5]).replace(',', '')),
                    'close': float(str(row[6]).replace(',', '')),
                    'volume': int(str(row[1]).replace(',', '')),
                })
            except:
                pass
    with cache_lock:
        static_cache[key] = rows
    return rows

# ── Yahoo Finance helpers ─────────────────────────────────────────────────────

def yf_get_quote(symbols):
    key = 'yf_' + '_'.join(symbols[:4])
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    results = []
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            hist = t.history(period='2d', interval='1d')
            price = float(info.last_price or 0)
            prev = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else float(info.previous_close or 0)
            chg = price - prev
            chg_pct = round(chg / prev * 100, 2) if prev else 0
            results.append({
                'symbol': sym,
                'name': sym,
                'price': round(price, 2),
                'change': round(chg, 2),
                'change_pct': chg_pct,
                'volume': int(info.three_month_average_volume or 0),
                'market': 'US',
            })
        except Exception as e:
            logger.error(f"yf error {sym}: {e}")
    with cache_lock:
        rt_cache[key] = results
    return results

def yf_get_history(symbol, period='1mo', interval='1d'):
    key = f'yf_hist_{symbol}_{period}_{interval}'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=period, interval=interval)
        rows = []
        for dt, row in hist.iterrows():
            rows.append({
                'date': dt.strftime('%Y-%m-%d %H:%M') if interval != '1d' else dt.strftime('%Y-%m-%d'),
                'open': round(float(row['Open']), 4),
                'high': round(float(row['High']), 4),
                'low': round(float(row['Low']), 4),
                'close': round(float(row['Close']), 4),
                'volume': int(row['Volume']),
            })
        with cache_lock:
            static_cache[key] = rows
        return rows
    except Exception as e:
        logger.error(f"yf history error {symbol}: {e}")
        return []

def get_forex():
    key = 'forex'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    pairs = ['USDTWD=X', 'EURUSD=X', 'USDJPY=X', 'GBPUSD=X', 'AUDUSD=X', 'USDCNY=X']
    names = {'USDTWD=X': 'USD/TWD', 'EURUSD=X': 'EUR/USD', 'USDJPY=X': 'USD/JPY',
             'GBPUSD=X': 'GBP/USD', 'AUDUSD=X': 'AUD/USD', 'USDCNY=X': 'USD/CNY'}
    result = []
    for sym in pairs:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            chg = price - prev
            result.append({
                'symbol': names.get(sym, sym),
                'price': round(price, 4),
                'change': round(chg, 4),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
            })
        except:
            pass
    with cache_lock:
        rt_cache[key] = result
    return result

def get_crypto():
    key = 'crypto'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    coins = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'BNB-USD', 'ADA-USD', 'XRP-USD']
    result = []
    for sym in coins:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            chg = price - prev
            result.append({
                'symbol': sym.replace('-USD', ''),
                'price': round(price, 2),
                'change': round(chg, 2),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
                'market_cap': int(info.market_cap or 0),
            })
        except:
            pass
    with cache_lock:
        rt_cache[key] = result
    return result

def get_us_indices():
    key = 'us_indices'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    indices = {'^GSPC': 'S&P 500', '^DJI': '道瓊工業', '^IXIC': '那斯達克',
               '^RUT': '羅素2000', '^VIX': '恐慌指數', '^TNX': '10年期美債'}
    result = []
    for sym, name in indices.items():
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            chg = price - prev
            result.append({
                'symbol': sym,
                'name': name,
                'price': round(price, 2),
                'change': round(chg, 2),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
            })
        except:
            pass
    with cache_lock:
        rt_cache[key] = result
    return result

def get_commodities():
    key = 'commodities'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    comms = {'GC=F': '黃金', 'SI=F': '白銀', 'CL=F': 'WTI原油', 'BZ=F': 'Brent原油',
             'NG=F': '天然氣', 'HG=F': '銅', 'ZW=F': '小麥', 'ZC=F': '玉米'}
    result = []
    for sym, name in comms.items():
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            chg = price - prev
            result.append({
                'symbol': sym,
                'name': name,
                'price': round(price, 2),
                'change': round(chg, 2),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
                'unit': 'USD',
            })
        except:
            pass
    with cache_lock:
        rt_cache[key] = result
    return result

def search_stock(query):
    """Search using Yahoo Finance"""
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=10&newsCount=0&enableFuzzyQuery=false&quotesQueryId=tss_match_phrase_query"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        data = r.json()
        results = []
        for q in data.get('quotes', []):
            results.append({
                'symbol': q.get('symbol', ''),
                'name': q.get('longname') or q.get('shortname', ''),
                'exchange': q.get('exchange', ''),
                'type': q.get('quoteType', ''),
            })
        return results
    except:
        return []

def get_news():
    key = 'news'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    news_list = []
    # Get news from Yahoo Finance for major indices
    try:
        t = yf.Ticker('^TWII')
        news = t.news
        for n in (news or [])[:8]:
            news_list.append({
                'title': n.get('title', ''),
                'link': n.get('link', ''),
                'source': n.get('publisher', ''),
                'time': datetime.fromtimestamp(n.get('providerPublishTime', time.time()), TW_TZ).strftime('%Y-%m-%d %H:%M'),
            })
    except:
        pass
    try:
        t2 = yf.Ticker('SPY')
        news2 = t2.news
        for n in (news2 or [])[:5]:
            news_list.append({
                'title': n.get('title', ''),
                'link': n.get('link', ''),
                'source': n.get('publisher', ''),
                'time': datetime.fromtimestamp(n.get('providerPublishTime', time.time()), TW_TZ).strftime('%Y-%m-%d %H:%M'),
            })
    except:
        pass
    with cache_lock:
        static_cache[key] = news_list
    return news_list

def get_tw_etfs():
    key = 'tw_etfs'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    etf_list = ['0050.TW', '0056.TW', '00878.TW', '00881.TW', '006208.TW', '00679B.TW', '00720B.TW', '00713.TW']
    result = []
    for sym in etf_list:
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            chg = price - prev
            full = t.info
            result.append({
                'symbol': sym.replace('.TW', ''),
                'name': full.get('longName') or full.get('shortName', sym),
                'price': round(price, 2),
                'change': round(chg, 2),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
                'volume': int(info.three_month_average_volume or 0),
            })
        except:
            pass
    with cache_lock:
        rt_cache[key] = result
    return result

def get_stock_detail(symbol):
    """Get full stock detail from yfinance"""
    key = f'detail_{symbol}'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    try:
        t = yf.Ticker(symbol)
        info = t.info
        fast = t.fast_info
        price = float(fast.last_price or 0)
        prev = float(fast.previous_close or 0)
        chg = price - prev
        result = {
            'symbol': symbol,
            'name': info.get('longName') or info.get('shortName', symbol),
            'price': round(price, 2),
            'change': round(chg, 2),
            'change_pct': round(chg / prev * 100, 2) if prev else 0,
            'open': round(float(fast.open or 0), 2),
            'high': round(float(fast.day_high or 0), 2),
            'low': round(float(fast.day_low or 0), 2),
            'prev_close': round(prev, 2),
            'volume': int(fast.last_volume or 0),
            'market_cap': int(fast.market_cap or 0),
            'pe_ratio': info.get('trailingPE', 0),
            'pb_ratio': info.get('priceToBook', 0),
            'eps': info.get('trailingEps', 0),
            'dividend_yield': round(float(info.get('dividendYield') or 0) * 100, 2),
            'week52_high': round(float(fast.year_high or 0), 2),
            'week52_low': round(float(fast.year_low or 0), 2),
            'sector': info.get('sector', ''),
            'industry': info.get('industry', ''),
            'description': info.get('longBusinessSummary', '')[:300] + '...' if info.get('longBusinessSummary') else '',
            'exchange': info.get('exchange', ''),
            'currency': info.get('currency', 'TWD'),
        }
        with cache_lock:
            static_cache[key] = result
        return result
    except Exception as e:
        logger.error(f"detail error {symbol}: {e}")
        return {}

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/market_overview')
def api_market_overview():
    try:
        taiex = get_taiex()
        us_indices = get_us_indices()
        now = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'taiex': taiex, 'us_indices': us_indices, 'updated': now})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tw_stocks')
def api_tw_stocks():
    page = request.args.get('page', 1, type=int)
    stocks = ['2330', '2317', '2454', '2382', '3711', '2308', '2303', '1301', '2881', '2882',
              '2886', '2891', '2892', '5880', '2002', '1303', '1326', '2412', '3008', '4938',
              '2379', '2395', '3045', '6446', '2327', '6505', '1216', '2207', '2357', '2474']
    data = get_twse_stocks(stocks)
    return jsonify({'data': data, 'total': len(data)})

@app.route('/api/us_stocks')
def api_us_stocks():
    symbols = ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'TSM', 'AVGO', 'AMD']
    data = yf_get_quote(symbols)
    return jsonify({'data': data})

@app.route('/api/etf')
def api_etf():
    data = get_tw_etfs()
    return jsonify({'data': data})

@app.route('/api/forex')
def api_forex():
    return jsonify({'data': get_forex()})

@app.route('/api/crypto')
def api_crypto():
    return jsonify({'data': get_crypto()})

@app.route('/api/commodities')
def api_commodities():
    return jsonify({'data': get_commodities()})

@app.route('/api/news')
def api_news():
    return jsonify({'data': get_news()})

@app.route('/api/stock/<symbol>/history')
def api_stock_history(symbol):
    period = request.args.get('period', '1mo')
    interval = request.args.get('interval', '1d')
    # TW stocks
    if symbol.isdigit() and len(symbol) == 4:
        data = get_stock_history_twse(symbol)
    else:
        data = yf_get_history(symbol, period=period, interval=interval)
    return jsonify({'data': data, 'symbol': symbol})

@app.route('/api/stock/<symbol>/detail')
def api_stock_detail(symbol):
    # For TW stocks, append .TW
    if symbol.isdigit() and len(symbol) == 4:
        sym = symbol + '.TW'
    else:
        sym = symbol
    data = get_stock_detail(sym)
    return jsonify({'data': data})

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'data': []})
    results = search_stock(q)
    return jsonify({'data': results})

@app.route('/api/top_tw')
def api_top_tw():
    """Top gainers/losers for TW market using yfinance"""
    key = 'top_tw'
    with cache_lock:
        if key in rt_cache:
            return jsonify(rt_cache[key])
    stocks = ['2330', '2317', '2454', '2382', '3711', '2308', '2303', '1301', '2881', '2882',
              '2886', '2891', '2892', '5880', '2002', '1303', '1326', '2412', '3008', '4938']
    data = get_twse_stocks(stocks)
    sorted_data = sorted(data, key=lambda x: x.get('change_pct', 0), reverse=True)
    result = {
        'gainers': [d for d in sorted_data if d['change_pct'] > 0][:5],
        'losers': [d for d in reversed(sorted_data) if d['change_pct'] < 0][:5],
    }
    with cache_lock:
        rt_cache[key] = result
    return jsonify(result)

@app.route('/api/heatmap')
def api_heatmap():
    """Sector heatmap data"""
    sectors = {
        '半導體': ['2330', '2454', '2303', '3711', '2379'],
        '金融': ['2881', '2882', '2886', '2891', '2892'],
        '電子': ['2317', '2382', '2308', '3008', '4938'],
        '傳產': ['1301', '1303', '1326', '2002', '1216'],
        '通訊': ['2412', '3045', '4904', '2498', '6415'],
    }
    all_stocks = []
    for sector, stocks in sectors.items():
        all_stocks.extend(stocks)
    data = get_twse_stocks(all_stocks)
    stock_map = {d['symbol']: d for d in data}
    result = []
    for sector, stocks in sectors.items():
        for sym in stocks:
            if sym in stock_map:
                s = stock_map[sym].copy()
                s['sector'] = sector
                result.append(s)
    return jsonify({'data': result})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True)

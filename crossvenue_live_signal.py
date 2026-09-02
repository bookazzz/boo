#!/usr/bin/env python3
"""Live public-data adapter for frozen Regime Hybrid v2.

Uses official Binance USD-M futures and Bybit V5 public market endpoints.
No authenticated account data. On any venue failure/staleness, emits data_fresh=false.
Monthly selection fetches the full current common USDT perpetual universe and exact
quote turnover from daily klines. Daily non-rebalance runs only need BTC regime.
"""
from __future__ import annotations

import argparse, json, math, os, re, statistics, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BINANCE = os.getenv("BINANCE_FAPI_BASE", "https://fapi.binance.com")
BYBIT = os.getenv("BYBIT_PUBLIC_BASE", "https://api.bybit.com")
EXCLUDE_BASE={'USDC','USDP','TUSD','FDUSD','BUSD','DAI','USDE','USDS','USD1','BFUSD','USUAL','USTC'}
SPECIAL_EXCLUDE={'BTCDOM','DEFI','FOOTBALL'}
TRADFI_RE=re.compile(r'^(AAPL|ADBE|AMD|AMZN|ASML|BABA|BRKB|COIN|COST|CRCL|CRM|CRWD|CSCO|DELL|DIS|DKNG|EBAY|GOOGL|HIMS|HOOD|HPE|IBM|INTC|IONQ|IREN|IWM|JPM|META|MSFT|MSTR|NFLX|NVDA|ORCL|PLTR|QQQ|SPY|TSLA|USO|EWJ|EWY|EWZ|HK0700|HK1810|HYUNDAI)$')


def crypto_ok(sym:str)->bool:
    if not sym.endswith('USDT'): return False
    b=sym[:-4]
    if b in EXCLUDE_BASE or b in SPECIAL_EXCLUDE or TRADFI_RE.match(b): return False
    if any(x in b for x in ('UP','DOWN','BULL','BEAR')) and not b.startswith(('JUP','PUMP')): return False
    return True

class PublicDataError(RuntimeError): pass

class Public:
    def __init__(self):
        self.s=requests.Session(); self.s.headers.update({'User-Agent':'regime-hybrid-v2-forward/1.0'})
    def get(self,url,params=None,timeout=20):
        last=None
        for i in range(4):
            try:
                r=self.s.get(url,params=params,timeout=timeout)
                if r.status_code==200:return r.json()
                last=PublicDataError(f'HTTP {r.status_code}: {r.text[:180]}')
            except Exception as e:last=e
            time.sleep(.4*(2**i))
        raise PublicDataError(str(last))

P=Public()

def binance_universe()->set[str]:
    x=P.get(BINANCE+'/fapi/v1/exchangeInfo'); out=set()
    for s in x.get('symbols',[]):
        sym=s.get('symbol','')
        if s.get('contractType')=='PERPETUAL' and s.get('quoteAsset')=='USDT' and s.get('status')=='TRADING' and crypto_ok(sym): out.add(sym)
    return out

def bybit_universe()->set[str]:
    out=set(); cursor=None
    while True:
        q={'category':'linear','limit':1000}
        if cursor:q['cursor']=cursor
        x=P.get(BYBIT+'/v5/market/instruments-info',q)
        if int(x.get('retCode',0))!=0: raise PublicDataError(str(x))
        res=x.get('result',{})
        for s in res.get('list',[]):
            sym=s.get('symbol','')
            if s.get('quoteCoin')=='USDT' and s.get('contractType') in {'LinearPerpetual','InversePerpetual'} and s.get('status')=='Trading' and crypto_ok(sym): out.add(sym)
        cursor=res.get('nextPageCursor') or ''
        if not cursor:break
    return out

def bklines(sym:str,limit=121):
    x=P.get(BINANCE+'/fapi/v1/klines',{'symbol':sym,'interval':'1d','limit':limit})
    rows=[]
    for r in x:
        rows.append({'ts':int(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'base_volume':float(r[5]),'quote_volume':float(r[7])})
    return rows

def yklines(sym:str,limit=121):
    x=P.get(BYBIT+'/v5/market/kline',{'category':'linear','symbol':sym,'interval':'D','limit':limit})
    if int(x.get('retCode',0))!=0: raise PublicDataError(str(x))
    rows=[]
    for r in x.get('result',{}).get('list',[]):
        # startTime, open, high, low, close, volume, turnover
        rows.append({'ts':int(r[0]),'open':float(r[1]),'high':float(r[2]),'low':float(r[3]),'close':float(r[4]),'base_volume':float(r[5]),'quote_volume':float(r[6])})
    return sorted(rows,key=lambda z:z['ts'])

def closed(rows,signal_day:str):
    cutoff=int(datetime.fromisoformat(signal_day).replace(tzinfo=timezone.utc).timestamp()*1000)
    return [r for r in rows if r['ts']<=cutoff]

def metrics(rows,signal_day:str):
    r=closed(rows,signal_day)
    if len(r)<91:return None
    c=[x['close'] for x in r]
    r30=c[-1]/c[-31]-1; r90=c[-1]/c[-91]-1
    rets=[c[i]/c[i-1]-1 for i in range(len(c)-90,len(c))]
    vol=statistics.stdev(rets) if len(rets)>=2 else 0.0
    liq=sum(x['quote_volume'] for x in r[-30:])/30
    return {'r30':r30,'r90':r90,'vol90':vol,'liq30_quote_usdt':liq,'close':c[-1],'last_ts':r[-1]['ts']}

def regime(m):
    if not m:return 'neutral'
    return 'bull' if m['r30']>0 and m['r90']>0 else ('bear' if m['r30']<0 and m['r90']<0 else 'neutral')

def prev_utc_day(day:str)->str:
    return (datetime.fromisoformat(day).date()-timedelta(days=1)).isoformat()

def fetch_pair(sym,signal_day):
    return sym,metrics(bklines(sym),signal_day),metrics(yklines(sym),signal_day)

def build(day:str, full_universe:bool, state_path:str|None=None)->dict[str,Any]:
    sigday=prev_utc_day(day); errors=[]
    try:
        bm=metrics(bklines('BTCUSDT'),sigday); ym=metrics(yklines('BTCUSDT'),sigday)
        ba=regime(bm); ya=regime(ym); reg=ba if ba==ya and ba in {'bull','bear'} else 'neutral'
    except Exception as e:
        return {'signal_date':sigday,'data_fresh':False,'venue_a_ok':False,'venue_b_ok':False,'regime':'neutral','targets':[],'adapter_meta':{'error':repr(e)}}
    fresh_expected=int(datetime.fromisoformat(sigday).replace(tzinfo=timezone.utc).timestamp()*1000)
    fresh=bool(bm and ym and bm['last_ts']==fresh_expected and ym['last_ts']==fresh_expected)
    targets=[]; meta={'btc_binance':bm,'btc_bybit':ym,'binance_regime':ba,'bybit_regime':ya}
    if full_universe and fresh and reg in {'bull','bear'}:
        try:
            common=sorted(binance_universe() & bybit_universe())
            meta['common_universe_count']=len(common)
            rows=[]
            # Bound concurrency; any individual symbol failure merely removes that symbol and is recorded.
            with ThreadPoolExecutor(max_workers=6) as ex:
                fut={ex.submit(fetch_pair,s,sigday):s for s in common}
                for f in as_completed(fut):
                    s=fut[f]
                    try:
                        _,a,b=f.result(); rows.append((s,a,b))
                    except Exception as e: errors.append({'symbol':s,'error':repr(e)})
            cand=[]
            for s,a,b in rows:
                if not a or not b:continue
                if a['liq30_quote_usdt']<5_000_000 or b['liq30_quote_usdt']<5_000_000:continue
                if reg=='bull':
                    if s!='BTCUSDT' and a['r30']>0 and a['r90']>0 and b['r30']>0 and b['r90']>0 and a['vol90']>0 and b['vol90']>0:
                        cand.append((s,min(a['r90']/a['vol90'],b['r90']/b['vol90'])))
                else:
                    if s!='BTCUSDT' and a['r30']<0 and a['r90']<0 and b['r30']<0 and b['r90']<0 and a['r30']>=-.35 and b['r30']>=-.35:
                        cand.append((s,(a['r30']+b['r30'])/2))
            cand.sort(key=lambda x:x[1],reverse=True)
            if reg=='bull':
                if bm['liq30_quote_usdt']>=5_000_000 and ym['liq30_quote_usdt']>=5_000_000: targets.append({'side':'long','symbol':'BTCUSDT'})
                if cand:targets.append({'side':'long','symbol':cand[0][0]})
            else:targets=[{'side':'short','symbol':s} for s,_ in cand[:2]]
            meta['candidate_count']=len(cand); meta['symbol_errors']=errors[:100]; meta['symbol_error_count']=len(errors)
        except Exception as e:
            fresh=False; errors.append({'stage':'universe','error':repr(e)})
    elif state_path and Path(state_path).exists():
        try:
            st=json.loads(Path(state_path).read_text()); targets=st.get('targets',[])
        except Exception: pass
    # Add execution-day metadata on days 1-3 only. Reference is obtained immediately before shadow execution by ticker in execution engine.
    dom=int(day[-2:])
    for t in targets:
        t['execute_today']=dom in (1,2,3)
        t['tranche']=dom if dom in (1,2,3) else None
    return {'signal_date':sigday,'data_fresh':fresh,'venue_a_ok':True,'venue_b_ok':True,'regime':reg,'targets':targets,'adapter_meta':meta}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--utc-day',default=datetime.now(timezone.utc).date().isoformat()); p.add_argument('--full-universe',action='store_true'); p.add_argument('--state'); p.add_argument('--out',default='signal.json')
    a=p.parse_args(); x=build(a.utc_day,a.full_universe,a.state); Path(a.out).write_text(json.dumps(x,indent=2)); print(json.dumps(x,indent=2))
if __name__=='__main__':main()
